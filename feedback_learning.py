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

import db
from models import Confidence, DetectionResult, SourceItem, SourcePriority

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


@dataclass
class ScoreBreakdown:
    score: int
    parts: dict  # human-readable component -> value, for transparency/logging


def compute_alert_score(
    conn: sqlite3.Connection, det: DetectionResult, source: str = ""
) -> ScoreBreakdown:
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
    """Decide whether to send, applying mutes then the score threshold."""
    bd = compute_alert_score(conn, det, source=item.source)
    score, parts = bd.score, bd.parts

    if alerting.get("respect_muted_sources", True) and db.is_source_muted(conn, item.source):
        return AlertDecision(False, score, "muted_source", parts)
    if alerting.get("respect_muted_companies", True) and db.is_company_muted(conn, det.ticker):
        return AlertDecision(False, score, "muted_company", parts)

    if det.confidence == Confidence.LOW and not alerting.get("send_low_confidence", False):
        return AlertDecision(False, score, "low_confidence_disabled", parts)

    if det.source_priority == SourcePriority.SOCIAL_RUMOR.value:
        if not alerting.get("send_social_rumor", True):
            return AlertDecision(False, score, "social_rumor_disabled", parts)
        if score < alerting.get("social_rumor_min_score", 70):
            return AlertDecision(False, score, "below_social_rumor_score", parts)

    if score < alerting.get("min_alert_score", 60):
        return AlertDecision(False, score, "below_min_score", parts)

    return AlertDecision(True, score, "", parts)
