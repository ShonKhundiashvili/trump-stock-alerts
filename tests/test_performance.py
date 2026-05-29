"""Tests for signal performance tracking (scorecard + outcome backfill)."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

import pytest

import db
import performance
from models import Confidence, DetectionResult, SourceItem, now_iso


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def _perf_row(conn, detection_id, ticker, direction, ret_3d, source="rss:WH",
              entry=100.0):
    conn.execute(
        """INSERT INTO signal_performance
           (detection_id, ticker, source, matched_phrase, direction, alert_date,
            entry_price, ret_1d, ret_3d, ret_7d, updated_at)
           VALUES (?, ?, ?, NULL, ?, '2026-05-20', ?, NULL, ?, NULL, ?)""",
        (detection_id, ticker, source, direction, entry, ret_3d, now_iso()),
    )
    conn.commit()


# --- summary -------------------------------------------------------------- #
def test_summary_no_data(conn):
    out = performance.summary(conn)
    assert "No matured outcomes" in out


def test_summary_hit_rate_and_avg(conn):
    _perf_row(conn, 1, "AAA", "bullish", 0.05)   # hit
    _perf_row(conn, 2, "BBB", "bullish", 0.03)   # hit
    _perf_row(conn, 3, "CCC", "bullish", -0.02)  # miss
    _perf_row(conn, 4, "DDD", "bullish", 0.10)   # hit
    out = performance.summary(conn, horizon="ret_3d")
    assert "Samples: 4" in out
    assert "3/4 (75%)" in out
    assert "Best: DDD +10.0%" in out
    assert "Worst: CCC -2.0%" in out


def test_summary_bearish_direction_counts_down_as_hit(conn):
    _perf_row(conn, 1, "SHRT", "bearish", -0.04)  # went down -> hit for a short
    _perf_row(conn, 2, "SHRT2", "bearish", 0.04)  # went up   -> miss for a short
    out = performance.summary(conn, horizon="ret_3d")
    assert "1/2 (50%)" in out


def test_summary_per_source_breakdown(conn):
    _perf_row(conn, 1, "AAA", "bullish", 0.05, source="rss:WH")
    _perf_row(conn, 2, "BBB", "bullish", 0.04, source="rss:WH")
    _perf_row(conn, 3, "CCC", "bullish", -0.01, source="rss:WH")
    out = performance.summary(conn, horizon="ret_3d")
    assert "By source" in out
    assert "rss:" in out or "rss" in out


# --- update_outcomes (with a faked yfinance) ------------------------------ #
def _insert_sent_alert(conn, ticker="MSFT"):
    item = SourceItem(source="rss:WH", source_item_id="x1",
                      url="http://e/x", text="buy MSFT", timestamp=now_iso())
    rowid = db.insert_source_item(conn, item)
    det = DetectionResult(
        company_name="Microsoft", ticker=ticker, candidate_tickers=[ticker],
        confidence=Confidence.HIGH, ticker_resolution_confidence=99.0,
        matched_phrase="buy", text_excerpt="buy MSFT", direction="bullish",
    )
    did = db.insert_detection(conn, item, det, rowid)
    db.mark_alert_sent(conn, did)
    return did


def _fake_yfinance():
    import pandas as pd
    today = datetime.now(timezone.utc).date()
    # Business days spanning before today through well after, rising 1/day.
    idx = pd.date_range(end=pd.Timestamp(today) + pd.Timedelta(days=16),
                        periods=30, freq="B")
    closes = [100.0 + i for i in range(len(idx))]
    df = pd.DataFrame({"Close": closes, "High": closes, "Low": closes}, index=idx)

    class _Ticker:
        def __init__(self, sym):
            self.sym = sym

        def history(self, period=None):
            return df

    mod = types.ModuleType("yfinance")
    mod.Ticker = _Ticker
    return mod


def test_update_outcomes_fills_forward_returns(conn, monkeypatch):
    did = _insert_sent_alert(conn, "MSFT")
    monkeypatch.setitem(sys.modules, "yfinance", _fake_yfinance())

    n = performance.update_outcomes(conn)
    assert n == 1

    row = conn.execute(
        "SELECT * FROM signal_performance WHERE detection_id = ?", (did,)
    ).fetchone()
    assert row is not None
    assert row["ticker"] == "MSFT"
    assert row["entry_price"] > 0
    # Rising series -> all forward returns positive.
    assert row["ret_1d"] > 0
    assert row["ret_3d"] > 0
    assert row["ret_7d"] > 0
    # +7d on a +1/day-from-entry series is a bigger move than +1d.
    assert row["ret_7d"] > row["ret_1d"]


def test_update_outcomes_skips_when_already_filled(conn, monkeypatch):
    _insert_sent_alert(conn, "MSFT")
    monkeypatch.setitem(sys.modules, "yfinance", _fake_yfinance())
    assert performance.update_outcomes(conn) == 1
    # Second run: ret_7d already populated -> nothing pending.
    assert performance.update_outcomes(conn) == 0


def test_update_outcomes_ignores_unsent_or_tickerless(conn, monkeypatch):
    # Unsent detection (alert_sent=0) and one without a ticker -> ignored.
    item = SourceItem(source="rss:WH", source_item_id="y1", url="http://e/y",
                      text="t", timestamp=now_iso())
    rowid = db.insert_source_item(conn, item)
    det = DetectionResult(company_name="X", ticker="ZZZZ", candidate_tickers=["ZZZZ"],
                          confidence=Confidence.HIGH, ticker_resolution_confidence=99.0,
                          matched_phrase="buy", text_excerpt="t", direction="bullish")
    db.insert_detection(conn, item, det, rowid)  # not marked sent
    monkeypatch.setitem(sys.modules, "yfinance", _fake_yfinance())
    assert performance.update_outcomes(conn) == 0
