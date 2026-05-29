"""Rule-based, transparent learning from human feedback.

This module turns stored Telegram feedback into deterministic, explainable
adjustments to alert scoring/filtering. It does NOT train a model, does NOT
rewrite code or config, and does NOT trade. It only:

  - computes an alert_score (0-100) from confidence, provenance, and the user's
    accumulated feedback about sources / companies / phrases, and
  - decides whether an alert should be sent (mutes + score threshold).

Everything here is a pure function of the DB + the detection, so the same inputs
always produce the same score, and you can inspect exactly why.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional, Tuple

import alert_policy
import db
from models import Confidence, DetectionResult, SourceItem, SourcePriority

# Penalty applied to an uncorroborated SECONDARY/SOCIAL claim with strong buy
# language — a single unreliable "Breaking: buy X" should not score like a
# confirmed call.
UNCORROBORATED_PENALTY = 15

# How each feedback label reflects on quality. Positive = good signal.
LABEL_WEIGHTS = {
    "useful": 1.0,
    "fake": -2.0,
    "not_useful": -1.0,
    "needs_context": -0.5,
    "too_late": 0.0,   # latency problem, not a correctness problem
    "training": 0.0,
    "mute_source": 0.0,
    "mute_company": 0.0,
}

# Base score by final confidence.
BASE_SCORE = {Confidence.HIGH: 85, Confidence.MEDIUM: 60, Confidence.LOW: 30}

# Provenance adjustments.
PRIORITY_ADJ = {
    SourcePriority.PRIMARY.value: 10,
    SourcePriority.SECONDARY.value: 0,
    SourcePriority.SOCIAL_RUMOR.value: -15,
}

# Bounds for the user-feedback adjustments.
SOURCE_CAP = 20.0
COMPANY_CAP = 15.0
PHRASE_CAP = 10.0


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _net_weight(counts: dict) -> float:
    return sum(LABEL_WEIGHTS.get(label, 0.0) * n for label, n in counts.items())


def source_quality_adjustment(conn: sqlite3.Connection, source: str) -> float:
    """-20..+20 from the user's feedback history for this source."""
    net = _net_weight(db.feedback_label_counts(conn, "source", source))
    return _clamp(net * 4.0, -SOURCE_CAP, SOURCE_CAP)


def company_relevance_adjustment(conn: sqlite3.Connection, ticker: Optional[str]) -> float:
    """-15..+15 from the user's feedback history for this ticker."""
    if not ticker:
        return 0.0
    net = _net_weight(db.feedback_label_counts(conn, "ticker", ticker))
    return _clamp(net * 4.0, -COMPANY_CAP, COMPANY_CAP)


def phrase_quality_adjustment(conn: sqlite3.Connection, phrase: Optional[str]) -> float:
    """-10..+10 from the user's feedback history for this matched phrase."""
    if not phrase:
        return 0.0
    net = _net_weight(db.phrase_label_counts(conn, phrase))
    return _clamp(net * 3.0, -PHRASE_CAP, PHRASE_CAP)


def is_uncorroborated(det: DetectionResult) -> bool:
    """True when a SECONDARY/SOCIAL claim lacks cross-source confirmation.

    Uncorroborated = comes from a SECONDARY or SOCIAL_RUMOR source AND no PRIMARY
    source carries it AND fewer than 2 independent secondary sources reported it.
    Relies on det.primary_source_found / det.corroborating_sources, which
    alert_policy.evaluate() sets before evaluate_alert() runs (see main.process_item).
    """
    if det.source_priority not in (
        SourcePriority.SECONDARY.value,
        SourcePriority.SOCIAL_RUMOR.value,
    ):
        return False
    return not det.primary_source_found and det.corroborating_sources < 2


@dataclass
class ScoreBreakdown:
    score: int
    parts: dict  # human-readable component -> value, for transparency/logging


def compute_alert_score(
    conn: sqlite3.Connection,
    det: DetectionResult,
    source: str = "",
    alerting: Optional[dict] = None,
) -> ScoreBreakdown:
    alerting = alerting or {}
    parts = {}
    score = float(BASE_SCORE.get(det.confidence, 30))
    parts["base(%s)" % det.confidence.value] = score

    pri = PRIORITY_ADJ.get(det.source_priority, 0)
    parts["priority(%s)" % det.source_priority] = pri
    score += pri

    # Clear direct buy/ownership language (text classified HIGH).
    text_conf = det.text_confidence or det.confidence
    if text_conf == Confidence.HIGH:
        parts["direct_buy_phrase"] = 15
        score += 15

    # Strong buy language from an unverified SECONDARY/SOCIAL source: penalize so
    # an unconfirmed "Breaking: buy X" can't score like a corroborated call.
    if (
        alerting.get("penalize_uncorroborated", True)
        and text_conf == Confidence.HIGH
        and is_uncorroborated(det)
    ):
        parts["uncorroborated_strong_claim"] = -UNCORROBORATED_PENALTY
        score -= UNCORROBORATED_PENALTY

    if det.ambiguous:
        parts["ambiguous_ticker"] = -20
        score -= 20

    sq = source_quality_adjustment(conn, source)
    cq = company_relevance_adjustment(conn, det.ticker)
    pq = phrase_quality_adjustment(conn, det.matched_phrase)
    parts["user_source_quality"] = round(sq, 1)
    parts["user_company_relevance"] = round(cq, 1)
    parts["user_phrase_quality"] = round(pq, 1)
    score += sq + cq + pq

    return ScoreBreakdown(int(round(_clamp(score, 0, 100))), parts)


@dataclass
class AlertDecision:
    send: bool
    score: int
    reason: str           # "" when sending; otherwise the suppression reason
    breakdown: dict


def evaluate_alert(
    conn: sqlite3.Connection,
    det: DetectionResult,
    item: SourceItem,
    alerting: dict,
) -> AlertDecision:
    """Decide whether to send, applying mutes, recency, verification, threshold."""
    bd = compute_alert_score(conn, det, source=item.source, alerting=alerting)
    score, parts = bd.score, bd.parts

    if alerting.get("respect_muted_sources", True) and db.is_source_muted(conn, item.source):
        return AlertDecision(False, score, "muted_source", parts)
    if alerting.get("respect_muted_companies", True) and db.is_company_muted(conn, det.ticker):
        return AlertDecision(False, score, "muted_company", parts)

    # Recency: drop historical items (common on first run when feeds carry old
    # entries). If the timestamp is missing/unparseable we treat it as fresh —
    # we can't tell, so we don't suppress.
    max_age = alerting.get("max_age_hours", 48)
    if max_age:
        age = alert_policy.age_hours(item.timestamp)
        if age is not None and age > max_age:
            return AlertDecision(False, score, "stale", parts)

    # Anti-fake: an unverified single social post making a strong claim should
    # not alert as if confirmed.
    if (
        alerting.get("social_requires_corroboration", True)
        and det.source_priority == SourcePriority.SOCIAL_RUMOR.value
        and is_uncorroborated(det)
    ):
        return AlertDecision(False, score, "unverified_social", parts)

    # Cross-source verification gate — the core "intersect sources" rule:
    #   * PRIMARY (Trump's own video/post/transcript) self-verifies → allowed.
    #   * Any other claim must be CORROBORATED: either a PRIMARY source also
    #     carries this ticker, OR >= min_independent_sources distinct sources
    #     reported it (within the corroboration window). Otherwise it is HELD as
    #     "awaiting_corroboration" (stored, not sent) — a lone unconfirmed
    #     "Breaking: Trump said buy X" never alerts unless others confirm it.
    if (
        alerting.get("require_corroboration", True)
        and det.source_priority != SourcePriority.PRIMARY.value
    ):
        min_sources = int(alerting.get("min_independent_sources", 2))
        verified = det.primary_source_found or det.corroborating_sources >= min_sources
        if not verified:
            return AlertDecision(False, score, "awaiting_corroboration", parts)

    if det.confidence == Confidence.LOW and not alerting.get("send_low_confidence", False):
        return AlertDecision(False, score, "low_confidence_disabled", parts)

    if det.source_priority == SourcePriority.SOCIAL_RUMOR.value:
        if not alerting.get("send_social_rumor", True):
            return AlertDecision(False, score, "social_rumor_disabled", parts)
        if score < alerting.get("social_rumor_min_score", 70):
            return AlertDecision(False, score, "below_social_rumor_score", parts)

    if score < alerting.get("min_alert_score", 60):
        return AlertDecision(False, score, "below_min_score", parts)

    # One alert per ticker within the cooldown, so a verified event doesn't spam
    # (many outlets reporting the same call would otherwise each fire).
    cooldown = int(alerting.get("ticker_cooldown_hours", 0) or 0)
    if cooldown and db.recent_alert_for_ticker(conn, det.ticker, cooldown):
        return AlertDecision(False, score, "ticker_cooldown", parts)

    return AlertDecision(True, score, "", parts)
