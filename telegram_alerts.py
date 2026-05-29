"""Telegram alerting.

Sends a formatted alert via the Telegram Bot API using TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID. Every alert includes the original source link and a clear
"not financial advice" disclaimer.
"""

from __future__ import annotations

import html
import logging
from typing import List, Optional, Tuple

import requests

import alert_policy
from models import DetectionResult, SourceItem

logger = logging.getLogger(__name__)

DISCLAIMER = "Not financial advice. Verify the source before acting."
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# (emoji label, callback action) — callback_data is "feedback:<detection_id>:<action>".
FEEDBACK_BUTTONS = [
    [("✅ Useful / Real Signal", "useful"), ("❌ Fake / Wrong", "fake")],
    [("⚠️ Real but Not Useful", "not_useful"), ("🧵 Needs More Context", "needs_context")],
    [("🚫 Mute This Source", "mute_source"), ("🔕 Mute This Company", "mute_company")],
    [("📈 Too Late", "too_late"), ("🧪 Mark as Training Example", "training")],
]


def build_feedback_keyboard(detection_id: int) -> dict:
    """Inline keyboard with compact callback payloads (well under Telegram's 64 bytes)."""
    rows = [
        [{"text": label, "callback_data": f"feedback:{detection_id}:{action}"}
         for (label, action) in row]
        for row in FEEDBACK_BUTTONS
    ]
    return {"inline_keyboard": rows}


class TelegramAlerter:
    def __init__(self, bot_token: Optional[str], chat_id: Optional[str],
                 timeout: int = 15, enable_feedback: bool = True) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.enable_feedback = enable_feedback

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def format_message(self, item: SourceItem, detection: DetectionResult) -> str:
        company = detection.company_name or "(unresolved)"
        ticker = detection.ticker or "?"
        candidates = [c for c in detection.candidate_tickers if c and c != ticker]
        candidate_line = ""
        if detection.ambiguous and candidates:
            candidate_line = f"\nCandidate tickers: {', '.join(candidates[:5])}"
        matched = detection.matched_phrase or "(company/ticker mention only)"

        # HTML parse mode; escape all dynamic content.
        e = html.escape
        text_conf = (detection.text_confidence or detection.confidence)
        lines = [
            "🚨 <b>Possible stock-related Trump mention</b>",
            "",
            f"<b>Company:</b> {e(company)}",
            f"<b>Ticker:</b> {e(ticker)}" + (f"  ({e(detection.in_index)})" if detection.in_index else ""),
            f"<b>Source:</b> {e(item.source)}",
            f"<b>Source priority:</b> {e(detection.source_priority)}",
            f"<b>Confidence:</b> {e(detection.confidence.value)}",
            f"<b>Verification:</b> {e(detection.verification_status or 'n/a')}",
            f"<b>Primary source found:</b> {'Yes' if detection.primary_source_found else 'No'}",
            f"<b>Ticker match confidence:</b> {detection.ticker_resolution_confidence:.0f}",
            f"<b>Matched phrase:</b> {e(str(matched))}",
            f"<b>Text:</b> {e(detection.text_excerpt)}",
            f"<b>Time:</b> {e(item.timestamp)}",
        ]
        if detection.corroborating_sources > 1:
            lines.append(f"<b>Independent news sources:</b> {detection.corroborating_sources}")
        if text_conf and text_conf != detection.confidence:
            lines.append(f"<b>Text classification:</b> {e(text_conf.value)}")
        if detection.detected_via:
            lines.append(f"<b>Detected via:</b> {e(detection.detected_via)}")
        if detection.llm_used and detection.llm_reason:
            lines.append(f"<b>LLM note:</b> {e(detection.llm_reason)}")
        if candidate_line:
            lines.append(candidate_line.strip())
        lines.append(f"<b>Link:</b> {e(item.url)}")

        warning = alert_policy.social_warning_for(detection)
        if warning:
            lines += ["", f"<b>{e(warning)}</b>"]
        lines += ["", f"<i>{DISCLAIMER}</i>"]
        return "\n".join(lines)

    def send(
        self,
        item: SourceItem,
        detection: DetectionResult,
        detection_id: Optional[int] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Send an alert. Returns (sent_ok, telegram_message_id, chat_id).

        When feedback is enabled and a detection_id is given, inline feedback
        buttons are attached so you can classify the alert from Telegram.
        """
        message = self.format_message(item, detection)
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        if self.enable_feedback and detection_id is not None:
            payload["reply_markup"] = build_feedback_keyboard(detection_id)

        if not self.enabled:
            logger.info("Telegram disabled; would have sent:\n%s", message)
            return (False, None, None)
        try:
            resp = requests.post(
                TELEGRAM_API.format(token=self.bot_token),
                json=payload,
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                logger.error("Telegram send failed (%s): %s", resp.status_code, resp.text)
                return (False, None, None)
            result = resp.json().get("result", {})
            message_id = str(result.get("message_id")) if result.get("message_id") else None
            chat_id = str(result.get("chat", {}).get("id")) if result.get("chat") else self.chat_id
            return (True, message_id, chat_id)
        except requests.RequestException as exc:
            logger.error("Telegram send error: %s", exc)
            return (False, None, None)
