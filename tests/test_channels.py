"""Tests for per-category Telegram channel routing."""

import json
from pathlib import Path

import db
from alert_policy import assign_channel
from feedback_bot import FeedbackBot
from models import DetectionResult, SourceItem, Confidence
from telegram_alerts import TelegramAlerter

BASE_DIR = Path(__file__).resolve().parent.parent

with (BASE_DIR / "config" / "channels.json").open() as fh:
    CHANNELS = json.load(fh)


def test_assign_channel_routes():
    assert assign_channel("rss:White House — News", CHANNELS) == "trump"
    assert assign_channel("youtube:White House", CHANNELS) == "trump"
    assert assign_channel("news_search:Trump buy stock", CHANNELS) == "trump"
    assert assign_channel("usaspending:federal contracts", CHANNELS) == "contracts"
    assert assign_channel("reddit:wsb", CHANNELS) == "social"
    assert assign_channel("rss:CNBC — Top News", CHANNELS) == "markets"
    assert assign_channel("news_search:Fed Powell stock market", CHANNELS) == "markets"
    # Unknown -> default channel.
    assert assign_channel("something:else", CHANNELS) == CHANNELS["default_channel"]


def test_alerter_routes_to_channel_chat():
    al = TelegramAlerter("tok", "DEFAULT", channel_chats={"trump": "T", "contracts": "C"})
    assert al.chat_for("trump") == "T"
    assert al.chat_for("contracts") == "C"
    # Channel without its own chat -> default.
    assert al.chat_for("markets") == "DEFAULT"
    assert al.chat_for("default") == "DEFAULT"


def test_send_uses_channel_chat(monkeypatch):
    al = TelegramAlerter("tok", "DEFAULT", enable_feedback=False,
                         channel_chats={"contracts": "CONTRACTS_CHAT"})
    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"result": {"message_id": 5, "chat": {"id": "CONTRACTS_CHAT"}}}

    def fake_post(url, json=None, timeout=None):
        captured["chat_id"] = json["chat_id"]
        return _Resp()

    monkeypatch.setattr("telegram_alerts.requests.post", fake_post)
    item = SourceItem(source="usaspending:x", source_item_id="1", url="http://x",
                      text="..", timestamp="2026-05-29T10:00:00Z", channel="contracts")
    det = DetectionResult(company_name="Dell", ticker="DELL", candidate_tickers=["DELL"],
                          confidence=Confidence.MEDIUM, ticker_resolution_confidence=98.0,
                          matched_phrase="contract", text_excerpt="x")
    al.send(item, det)
    assert captured["chat_id"] == "CONTRACTS_CHAT"


def test_feedback_authorizes_all_channel_chats():
    c = db.connect(":memory:")
    db.init_db(c)
    bot = FeedbackBot("tok", "MAIN", c, extra_chat_ids=["T", "C"])
    assert bot._authorized("MAIN")
    assert bot._authorized("T")
    assert bot._authorized("C")
    assert not bot._authorized("OTHER")
    c.close()
