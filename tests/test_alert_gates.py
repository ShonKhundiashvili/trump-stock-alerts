"""Integration tests for the timing gates in main.process_item:

  - already_ran: a buy signal whose stock already moved is suppressed.
  - stale_news : Trump-room SECONDARY (aftermath) news older than the tight
                 window is suppressed, while fresh primary statements pass.

These drive process_item with a fake detector/alerter and a monkeypatched
market_data.quote (no network).
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest

import db
import main
import market_data
from models import Confidence, DetectionResult, SourceItem


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


class _FakeAlerter:
    enable_feedback = False

    def __init__(self):
        self.sent = []

    def has_dedicated_route(self, channel):
        return True

    def send(self, item, det, detection_id=None, alert_score=None):
        self.sent.append((item, det))
        return (True, "1", "chat")

    def send_notice(self, *a, **k):
        return True


class _FakeDetector:
    """Returns a fresh DetectionResult each call (process_item mutates it)."""

    def __init__(self, **det_kw):
        self.det_kw = det_kw

    def detect(self, text):
        base = dict(
            company_name="Dell Technologies Inc.", ticker="DELL",
            candidate_tickers=["DELL"], confidence=Confidence.HIGH,
            ticker_resolution_confidence=98.0, matched_phrase="go out and buy",
            text_excerpt=text, direction="bullish",
        )
        base.update(self.det_kw)
        return [DetectionResult(**base)]


_LLM = types.SimpleNamespace(enabled=False)
_SETTINGS = types.SimpleNamespace(account_size=10000, risk_pct=1.0)

# Permissive gating so we isolate the timing gates (not the score/corroboration ones).
_ALERTING = {
    "min_alert_score": 0, "send_low_confidence": True, "send_social_rumor": True,
    "social_rumor_min_score": 0, "respect_muted_sources": True,
    "respect_muted_companies": True, "max_age_hours": 72,
    "require_corroboration": False, "penalize_uncorroborated": False,
    "social_requires_corroboration": False,
    "max_recent_run_pct": 12, "trump_news_max_age_hours": 18,
}


def _iso(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _item(priority="PRIMARY", channel="trump", hours_ago=1, item_id="i1"):
    it = SourceItem(source="news_search:Trump says buy", source_item_id=item_id,
                    url="http://e/x", text="Trump said go out and buy DELL",
                    timestamp=_iso(hours_ago), priority=priority)
    it.channel = channel
    return it


def _last(conn):
    return conn.execute(
        "SELECT alert_sent, alert_suppressed_reason FROM detections ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _run(conn, item, monkeypatch, move_5d=0.0, move_1d=0.0, det_kw=None):
    q = market_data.Quote("DELL", 100.0, 2.0, 2.0, move_1d, move_5d)
    monkeypatch.setattr(market_data, "quote", lambda t, cache=None: q)
    detector = _FakeDetector(**(det_kw or {}))
    alerter = _FakeAlerter()
    main.process_item(conn, item, detector, _LLM, alerter, _ALERTING,
                      require_keywords=None, settings=_SETTINGS, quote_cache={})
    return alerter


# --- already_ran ---------------------------------------------------------- #
def test_already_ran_suppresses_big_move(conn, monkeypatch):
    alerter = _run(conn, _item(), monkeypatch, move_5d=0.61)   # DELL +61%
    assert alerter.sent == []
    row = _last(conn)
    assert row["alert_sent"] == 0
    assert row["alert_suppressed_reason"] == "already_ran"


def test_fresh_move_is_sent_with_trade_note(conn, monkeypatch):
    alerter = _run(conn, _item(), monkeypatch, move_5d=0.01)
    assert len(alerter.sent) == 1
    _, det = alerter.sent[0]
    assert "Plan (research)" in det.trade_note   # enrichment attached
    assert _last(conn)["alert_sent"] == 1


# --- stale_news ----------------------------------------------------------- #
def test_stale_secondary_trump_news_suppressed(conn, monkeypatch):
    # 30h-old SECONDARY news in the Trump room -> stale (window is 18h).
    item = _item(priority="SECONDARY", channel="trump", hours_ago=30)
    alerter = _run(conn, item, monkeypatch, move_5d=0.0)
    assert alerter.sent == []
    assert _last(conn)["alert_suppressed_reason"] == "stale_news"


def test_fresh_secondary_trump_news_passes(conn, monkeypatch):
    item = _item(priority="SECONDARY", channel="trump", hours_ago=3)
    alerter = _run(conn, item, monkeypatch, move_5d=0.0)
    assert len(alerter.sent) == 1


def test_primary_trump_statement_not_stale_after_window(conn, monkeypatch):
    # Trump's OWN primary statement at 30h is NOT subject to the tight news window.
    item = _item(priority="PRIMARY", channel="trump", hours_ago=30)
    alerter = _run(conn, item, monkeypatch, move_5d=0.0)
    assert len(alerter.sent) == 1


def test_stale_gate_scoped_to_trump_channel(conn, monkeypatch):
    # Same 30h SECONDARY item in the markets room is NOT hit by the trump-only gate.
    item = _item(priority="SECONDARY", channel="markets", hours_ago=30)
    alerter = _run(conn, item, monkeypatch, move_5d=0.0)
    assert len(alerter.sent) == 1
