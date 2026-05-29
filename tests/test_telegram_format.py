"""Tests for the concise Telegram alert formatting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from models import Confidence, DetectionResult, SourceItem
from telegram_alerts import TelegramAlerter, _relative_time


def _detection(**overrides) -> DetectionResult:
    base = dict(
        company_name="Dell Technologies Inc.",
        ticker="DELL",
        candidate_tickers=["DELL"],
        confidence=Confidence.HIGH,
        ticker_resolution_confidence=98.0,
        matched_phrase="go out and buy",
        text_excerpt="Go out and buy a Dell, they're great.",
        ambiguous=False,
        detected_via="watchlist",
        in_index="S&P 500",
        source_priority="PRIMARY",
        verification_status="CORROBORATED — primary source",
        primary_source_found=True,
        corroborating_sources=2,
    )
    base.update(overrides)
    return DetectionResult(**base)


def _item(**overrides) -> SourceItem:
    base = dict(
        source="rss:White House — News",
        source_item_id="abc123",
        url="https://example.com/original-source",
        text="Go out and buy a Dell, they're great.",
        timestamp=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        priority="PRIMARY",
    )
    base.update(overrides)
    return SourceItem(**base)


def _alerter() -> TelegramAlerter:
    return TelegramAlerter(bot_token=None, chat_id=None)


def test_message_is_concise_and_has_essentials():
    msg = _alerter().format_message(_item(), _detection(), alert_score=92)
    lines = msg.splitlines()
    assert len(lines) < 9, msg
    assert "DELL" in msg
    assert "Dell Technologies Inc." in msg
    assert "HIGH" in msg
    assert "https://example.com/original-source" in msg
    assert "(S&amp;P 500)" in msg  # escaped index tag


def test_dropped_verbose_labels_absent():
    msg = _alerter().format_message(_item(), _detection(), alert_score=92)
    assert "Detected via" not in msg
    assert "Ticker match confidence" not in msg
    assert "Primary source found" not in msg
    assert "Independent news sources" not in msg
    assert "Text classification" not in msg


def test_alert_score_shown_when_passed():
    with_score = _alerter().format_message(_item(), _detection(), alert_score=92)
    assert "score 92" in with_score
    without_score = _alerter().format_message(_item(), _detection())
    assert "score" not in without_score


def test_excerpt_is_trimmed():
    long_text = "buy " * 200  # well over the 180-char cap
    msg = _alerter().format_message(
        _item(), _detection(text_excerpt=long_text), alert_score=10
    )
    # The excerpt line should be capped; check no excessively long line.
    excerpt_lines = [ln for ln in msg.splitlines() if ln.startswith("💬")]
    assert excerpt_lines
    assert len(excerpt_lines[0]) < 220
    assert "…" in excerpt_lines[0]


def test_candidates_line_when_ambiguous():
    det = _detection(ambiguous=True, candidate_tickers=["DELL", "DLL", "DELLX"])
    msg = _alerter().format_message(_item(), det, alert_score=50)
    assert "Candidates:" in msg
    assert "DLL" in msg


def test_relative_time_recent_iso():
    ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert "ago" in _relative_time(ts)


def test_relative_time_z_suffix():
    ts = (datetime.now(timezone.utc) - timedelta(minutes=35)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert "ago" in _relative_time(ts)


def test_relative_time_rfc822():
    # RFC822/RFC1123 style; should parse without crashing and yield a relative form.
    out = _relative_time("Wed, 02 Oct 2002 13:00:00 GMT")
    assert isinstance(out, str)
    assert out  # non-empty


def test_relative_time_garbage_does_not_crash():
    assert _relative_time("not a real timestamp") == "not a real timestamp"
    assert _relative_time("") == ""
    assert _relative_time(None) == ""
