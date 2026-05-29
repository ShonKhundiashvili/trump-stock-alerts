"""Tests for the human-in-the-loop feedback system."""

import pytest

import db
import feedback_bot
import feedback_learning
from feedback_bot import FeedbackBot, parse_callback_data
from models import Confidence, DetectionResult, SourceItem


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def _store_detection(c, ticker="DELL", source="rss:White House — News",
                     priority="PRIMARY", confidence=Confidence.HIGH,
                     phrase="go out and buy", text_conf=Confidence.HIGH):
    item = SourceItem(source=source, source_item_id=f"itm-{ticker}",
                      url="https://example.com/x", text="Go out and buy a Dell.",
                      timestamp="2026-05-29T10:00:00Z", priority=priority)
    rowid = db.insert_source_item(c, item)
    det = DetectionResult(
        company_name="Dell Technologies Inc.", ticker=ticker, candidate_tickers=[ticker],
        confidence=confidence, ticker_resolution_confidence=98.0,
        matched_phrase=phrase, text_excerpt="Go out and buy a Dell.",
        source_priority=priority, text_confidence=text_conf,
    )
    det_id = db.insert_detection(c, item, det, rowid)
    return item, det, det_id


class CaptureBot(FeedbackBot):
    """FeedbackBot that records Telegram API calls instead of doing HTTP."""

    def __init__(self, conn, chat_id="111"):
        super().__init__("token", chat_id, conn)
        self.calls = []

    def _api(self, method, payload, timeout=None):
        self.calls.append((method, payload))
        return {"ok": True, "result": []}

    def sent_texts(self):
        return [p.get("text", "") for (m, p) in self.calls if m == "sendMessage"]

    def answers(self):
        return [p.get("text", "") for (m, p) in self.calls if m == "answerCallbackQuery"]


def _callback(det_id, action, chat_id="111", data=None):
    return {
        "id": "cb1",
        "from": {"id": 111, "username": "shon"},
        "message": {"message_id": 9, "chat": {"id": int(chat_id)}},
        "data": data or f"feedback:{det_id}:{action}",
    }


# 9. Callback payload parsing
def test_callback_parsing():
    assert parse_callback_data("feedback:42:useful") == (42, "useful")
    assert parse_callback_data("feedback:7:mute_source") == (7, "mute_source")
    assert parse_callback_data("feedback:x:useful") is None
    assert parse_callback_data("feedback:42:bogus") is None
    assert parse_callback_data("noop") is None
    assert parse_callback_data("") is None


# 1. Feedback DB insert (via callback handler)
def test_feedback_insert_via_callback(conn):
    _, _, det_id = _store_detection(conn)
    bot = CaptureBot(conn)
    bot.handle_callback(_callback(det_id, "useful"))
    fb = db.feedback_for_detection(conn, det_id)
    assert len(fb) == 1
    assert fb[0]["feedback_label"] == "useful"
    assert any("Useful" in a for a in bot.answers())


# 10. Security: unauthorized chat is ignored
def test_unauthorized_chat_ignored(conn):
    _, _, det_id = _store_detection(conn)
    bot = CaptureBot(conn, chat_id="111")
    bot.handle_callback(_callback(det_id, "useful", chat_id="999"))
    assert db.feedback_for_detection(conn, det_id) == []


def test_old_callback_does_not_crash(conn):
    bot = CaptureBot(conn)
    bot.handle_callback(_callback(999999, "useful"))  # detection doesn't exist
    assert any("no longer available" in a.lower() for a in bot.answers())


# 2. Muting a source suppresses future alerts
def test_mute_source_suppresses(conn):
    item, det, _ = _store_detection(conn)
    bot = CaptureBot(conn)
    _, det2, det_id2 = _store_detection(conn, ticker="INTC")
    bot.handle_callback(_callback(det_id2, "mute_source"))
    assert db.is_source_muted(conn, item.source)
    alerting = {"min_alert_score": 60, "respect_muted_sources": True,
                "respect_muted_companies": True, "send_low_confidence": True}
    decision = feedback_learning.evaluate_alert(conn, det, item, alerting)
    assert decision.send is False
    assert decision.reason == "muted_source"


# 3. Muting a company suppresses future alerts
def test_mute_company_suppresses(conn):
    item, det, det_id = _store_detection(conn)
    bot = CaptureBot(conn)
    bot.handle_callback(_callback(det_id, "mute_company"))
    assert db.is_company_muted(conn, "DELL")
    alerting = {"min_alert_score": 60, "respect_muted_sources": True,
                "respect_muted_companies": True}
    decision = feedback_learning.evaluate_alert(conn, det, item, alerting)
    assert decision.send is False
    assert decision.reason == "muted_company"


# 4. Useful feedback increases source/company score
def test_useful_increases_scores(conn):
    item, det, det_id = _store_detection(conn)
    bot = CaptureBot(conn)
    bot.handle_callback(_callback(det_id, "useful"))
    assert feedback_learning.source_quality_adjustment(conn, item.source) > 0
    assert feedback_learning.company_relevance_adjustment(conn, "DELL") > 0


# 5. Fake feedback decreases source score
def test_fake_decreases_source(conn):
    item, det, det_id = _store_detection(conn)
    bot = CaptureBot(conn)
    bot.handle_callback(_callback(det_id, "fake"))
    assert feedback_learning.source_quality_adjustment(conn, item.source) < 0


# 6. Not-useful feedback lowers phrase score
def test_not_useful_lowers_phrase(conn):
    item, det, det_id = _store_detection(conn, phrase="great company")
    bot = CaptureBot(conn)
    bot.handle_callback(_callback(det_id, "not_useful"))
    assert feedback_learning.phrase_quality_adjustment(conn, "great company") < 0


# 7. Alert score calculation
def test_alert_score_calculation(conn):
    _, det, _ = _store_detection(conn, confidence=Confidence.HIGH, priority="PRIMARY",
                                 text_conf=Confidence.HIGH)
    bd = feedback_learning.compute_alert_score(conn, det, source="rss:White House — News")
    # base 85 + primary 10 + direct-buy 15 = 110 -> clamped to 100
    assert bd.score == 100

    _, det_mid, _ = _store_detection(conn, ticker="MU", confidence=Confidence.MEDIUM,
                                     priority="SECONDARY", text_conf=Confidence.MEDIUM)
    bd2 = feedback_learning.compute_alert_score(conn, det_mid, source="rss:CNBC")
    assert bd2.score == 60  # base 60 + secondary 0


# 8. Alert threshold suppression
def test_threshold_suppression(conn):
    item, det, _ = _store_detection(conn, ticker="MU", confidence=Confidence.MEDIUM,
                                    priority="SECONDARY", text_conf=Confidence.MEDIUM)
    alerting = {"min_alert_score": 90, "respect_muted_sources": True,
                "respect_muted_companies": True, "send_social_rumor": True}
    decision = feedback_learning.evaluate_alert(conn, det, item, alerting)
    assert decision.send is False
    assert decision.reason == "below_min_score"


def test_high_primary_passes_threshold(conn):
    item, det, _ = _store_detection(conn)
    alerting = {"min_alert_score": 60, "respect_muted_sources": True,
                "respect_muted_companies": True}
    decision = feedback_learning.evaluate_alert(conn, det, item, alerting)
    assert decision.send is True
    assert decision.reason == ""


# 11. /stats command output
def test_stats_command(conn):
    _, _, det_id = _store_detection(conn)
    db.mark_alert_sent(conn, det_id)
    bot = CaptureBot(conn)
    bot.handle_callback(_callback(det_id, "useful"))
    bot.handle_command({"chat": {"id": 111}, "text": "/stats"})
    out = "\n".join(bot.sent_texts())
    assert "Stats" in out
    assert "Useful: 1" in out


# 12 & 13. unmute commands
def test_unmute_source_command(conn):
    db.mute_source(conn, "rss:CNBC — Top News", "test")
    bot = CaptureBot(conn)
    bot.handle_command({"chat": {"id": 111}, "text": "/unmute_source rss:CNBC — Top News"})
    assert not db.is_source_muted(conn, "rss:CNBC — Top News")
    assert any("Unmuted" in t for t in bot.sent_texts())


def test_unmute_company_command(conn):
    db.mute_company(conn, "DELL", "Dell", "test")
    bot = CaptureBot(conn)
    bot.handle_command({"chat": {"id": 111}, "text": "/unmute_company dell"})
    assert not db.is_company_muted(conn, "DELL")


def test_commands_require_authorized_chat(conn):
    db.mute_source(conn, "rss:CNBC — Top News", "test")
    bot = CaptureBot(conn, chat_id="111")
    bot.handle_command({"chat": {"id": 999}, "text": "/unmute_source rss:CNBC — Top News"})
    # Unauthorized -> still muted, no reply
    assert db.is_source_muted(conn, "rss:CNBC — Top News")
    assert bot.sent_texts() == []
