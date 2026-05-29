"""Tests for recency filtering and cross-source / anti-fake verification.

These import feedback_learning / alert_policy / db / models directly and
deliberately avoid importing main or telegram_alerts.
"""

from datetime import datetime, timedelta, timezone

import pytest

import alert_policy
import db
import feedback_learning
from models import Confidence, DetectionResult, SourceItem, SourcePriority


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _make(
    conn,
    *,
    ticker="TSLA",
    source="news_search:Trump buy",
    priority=SourcePriority.SECONDARY.value,
    confidence=Confidence.MEDIUM,
    text_conf=Confidence.HIGH,
    timestamp=None,
    primary_found=False,
    corroborating=1,
):
    if timestamp is None:
        timestamp = _iso(datetime.now(timezone.utc))
    item = SourceItem(
        source=source,
        source_item_id=f"itm-{ticker}-{timestamp}",
        url="https://example.com/x",
        text="Breaking: Trump said buy TSLA!",
        timestamp=timestamp,
        priority=priority,
    )
    db.insert_source_item(conn, item)
    det = DetectionResult(
        company_name="Tesla Inc.",
        ticker=ticker,
        candidate_tickers=[ticker],
        confidence=confidence,
        ticker_resolution_confidence=98.0,
        matched_phrase="said buy",
        text_excerpt="Breaking: Trump said buy TSLA!",
        source_priority=priority,
        text_confidence=text_conf,
        primary_source_found=primary_found,
        corroborating_sources=corroborating,
    )
    return item, det


ALERTING = {
    "min_alert_score": 60,
    "send_low_confidence": True,
    "send_social_rumor": True,
    "social_rumor_min_score": 0,
    "respect_muted_sources": True,
    "respect_muted_companies": True,
    "max_age_hours": 48,
    "social_requires_corroboration": True,
    "penalize_uncorroborated": True,
}


# --------------------------------------------------------------------------- #
# parse_timestamp / age_hours
# --------------------------------------------------------------------------- #
def test_parse_timestamp_rfc822():
    dt = alert_policy.parse_timestamp("Thu, 28 May 2026 10:59:16 GMT")
    assert dt is not None
    assert dt.tzinfo is not None
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 5, 28, 10)


def test_parse_timestamp_iso_z():
    dt = alert_policy.parse_timestamp("2026-05-29T10:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt == datetime(2026, 5, 29, 10, 0, 0, tzinfo=timezone.utc)


def test_parse_timestamp_iso_offset():
    dt = alert_policy.parse_timestamp("2026-05-29T10:00:00+00:00")
    assert dt == datetime(2026, 5, 29, 10, 0, 0, tzinfo=timezone.utc)


def test_parse_timestamp_garbage_returns_none():
    assert alert_policy.parse_timestamp("not a date") is None
    assert alert_policy.parse_timestamp("") is None
    assert alert_policy.parse_timestamp(None) is None


def test_age_hours():
    now = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
    assert alert_policy.age_hours("2026-05-29T10:00:00Z", now=now) == pytest.approx(2.0)
    assert alert_policy.age_hours("garbage", now=now) is None


# --------------------------------------------------------------------------- #
# staleness suppression
# --------------------------------------------------------------------------- #
def test_old_item_suppressed_as_stale(conn):
    old = _iso(datetime.now(timezone.utc) - timedelta(hours=72))
    item, det = _make(conn, priority=SourcePriority.PRIMARY.value,
                      confidence=Confidence.HIGH, timestamp=old)
    decision = feedback_learning.evaluate_alert(conn, det, item, ALERTING)
    assert decision.send is False
    assert decision.reason == "stale"


def test_fresh_item_not_stale(conn):
    fresh = _iso(datetime.now(timezone.utc) - timedelta(hours=2))
    item, det = _make(conn, priority=SourcePriority.PRIMARY.value,
                      confidence=Confidence.HIGH, timestamp=fresh)
    decision = feedback_learning.evaluate_alert(conn, det, item, ALERTING)
    assert decision.reason != "stale"
    assert decision.send is True


def test_missing_timestamp_not_suppressed(conn):
    item, det = _make(conn, priority=SourcePriority.PRIMARY.value,
                      confidence=Confidence.HIGH, timestamp="")
    decision = feedback_learning.evaluate_alert(conn, det, item, ALERTING)
    assert decision.reason != "stale"


# --------------------------------------------------------------------------- #
# cross-source / anti-fake verification
# --------------------------------------------------------------------------- #
def test_uncorroborated_social_suppressed(conn):
    item, det = _make(conn, source="reddit:wsb",
                      priority=SourcePriority.SOCIAL_RUMOR.value,
                      confidence=Confidence.LOW, text_conf=Confidence.HIGH,
                      primary_found=False, corroborating=0)
    decision = feedback_learning.evaluate_alert(conn, det, item, ALERTING)
    assert decision.send is False
    assert decision.reason == "unverified_social"


def test_corroborated_social_allowed(conn):
    # PRIMARY corroboration present -> no longer uncorroborated.
    item, det = _make(conn, source="reddit:wsb",
                      priority=SourcePriority.SOCIAL_RUMOR.value,
                      confidence=Confidence.LOW, text_conf=Confidence.HIGH,
                      primary_found=True, corroborating=1)
    decision = feedback_learning.evaluate_alert(conn, det, item, ALERTING)
    assert decision.reason != "unverified_social"


def test_uncorroborated_penalty_applied(conn):
    _, det = _make(conn, priority=SourcePriority.SECONDARY.value,
                   confidence=Confidence.MEDIUM, text_conf=Confidence.HIGH,
                   primary_found=False, corroborating=1)
    bd = feedback_learning.compute_alert_score(conn, det, alerting=ALERTING)
    assert bd.parts.get("uncorroborated_strong_claim") == -15


def test_no_penalty_when_corroborated(conn):
    _, det = _make(conn, priority=SourcePriority.SECONDARY.value,
                   confidence=Confidence.HIGH, text_conf=Confidence.HIGH,
                   primary_found=False, corroborating=2)
    bd = feedback_learning.compute_alert_score(conn, det, alerting=ALERTING)
    assert "uncorroborated_strong_claim" not in bd.parts


def test_penalty_disabled_by_config(conn):
    _, det = _make(conn, priority=SourcePriority.SECONDARY.value,
                   confidence=Confidence.MEDIUM, text_conf=Confidence.HIGH,
                   primary_found=False, corroborating=1)
    cfg = dict(ALERTING, penalize_uncorroborated=False)
    bd = feedback_learning.compute_alert_score(conn, det, alerting=cfg)
    assert "uncorroborated_strong_claim" not in bd.parts


# --------------------------------------------------------------------------- #
# cross-source verification gate (intersect sources before alerting)
# --------------------------------------------------------------------------- #
VGATE = dict(ALERTING, require_corroboration=True, min_independent_sources=2)


def test_lone_secondary_held_awaiting_corroboration(conn):
    # A single uncorroborated news claim is HELD, not sent.
    item, det = _make(conn, priority=SourcePriority.SECONDARY.value,
                      confidence=Confidence.MEDIUM, primary_found=False, corroborating=1)
    decision = feedback_learning.evaluate_alert(conn, det, item, VGATE)
    assert decision.send is False
    assert decision.reason == "awaiting_corroboration"


def test_two_independent_sources_alert(conn):
    item, det = _make(conn, priority=SourcePriority.SECONDARY.value,
                      confidence=Confidence.MEDIUM, primary_found=False, corroborating=2)
    decision = feedback_learning.evaluate_alert(conn, det, item, VGATE)
    assert decision.send is True


def test_secondary_with_primary_corroboration_alert(conn):
    item, det = _make(conn, priority=SourcePriority.SECONDARY.value,
                      confidence=Confidence.MEDIUM, primary_found=True, corroborating=1)
    decision = feedback_learning.evaluate_alert(conn, det, item, VGATE)
    assert decision.send is True


def test_primary_self_verifies(conn):
    # Trump's own source (PRIMARY) alerts without external corroboration.
    item, det = _make(conn, priority=SourcePriority.PRIMARY.value,
                      confidence=Confidence.HIGH, primary_found=False, corroborating=0)
    decision = feedback_learning.evaluate_alert(conn, det, item, VGATE)
    assert decision.send is True


def test_ticker_cooldown_suppresses_repeat(conn):
    # Once an alert for a ticker is recorded, a corroborated repeat is held.
    db.record_alert(conn, "rss:CNBC", "prev", "TSLA")
    item, det = _make(conn, priority=SourcePriority.SECONDARY.value,
                      confidence=Confidence.MEDIUM, primary_found=True, corroborating=2)
    cfg = dict(VGATE, ticker_cooldown_hours=6)
    decision = feedback_learning.evaluate_alert(conn, det, item, cfg)
    assert decision.send is False
    assert decision.reason == "ticker_cooldown"
