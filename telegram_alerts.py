"""Telegram alerting.

Sends a formatted alert via the Telegram Bot API using TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID. Every alert includes the original source link and a clear
"not financial advice" disclaimer.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional, Tuple

import requests

import alert_policy
from models import DetectionResult, SourceItem

logger = logging.getLogger(__name__)

DISCLAIMER = "Not financial advice. Verify the source before acting."
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

EXCERPT_CAP = 180

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


def _relative_time(timestamp: Optional[str]) -> str:
    """Return a short relative time ("2h ago") for a parseable timestamp.

    Handles ISO8601 (with trailing "Z" or "+00:00") and RFC822/RFC1123
    (via email.utils.parsedate_to_datetime). Falls back to the raw string
    if parsing fails. Never raises.
    """
    if not timestamp:
        return ""
    raw = str(timestamp).strip()
    dt: Optional[datetime] = None
    # Try ISO8601 first (normalize a trailing Z to a UTC offset).
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        dt = None
    # Fall back to RFC822 / RFC1123.
    if dt is None:
        try:
            dt = parsedate_to_datetime(raw)
        except (ValueError, TypeError, IndexError):
            dt = None
    if dt is None:
        return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        delta = datetime.now(timezone.utc) - dt
    except (OverflowError, OSError):
        return raw
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


class TelegramAlerter:
    def __init__(self, bot_token: Optional[str], chat_id: Optional[str],
                 timeout: int = 15, enable_feedback: bool = True,
                 channel_chats: Optional[dict] = None,
                 channel_threads: Optional[dict] = None) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.enable_feedback = enable_feedback
        # channel -> separate chat id (optional); falls back to chat_id.
        self.channel_chats = channel_chats or {}
        # channel -> forum topic id within the main group (one group, sub-threads).
        self.channel_threads = channel_threads or {}

    def chat_for(self, channel: Optional[str]) -> Optional[str]:
        """Target chat id for a routing channel (falls back to the default chat)."""
        if channel and channel in self.channel_chats:
            return self.channel_chats[channel]
        return self.chat_id

    def has_dedicated_route(self, channel: Optional[str]) -> bool:
        """True if this channel has its own chat OR its own forum topic."""
        return bool(channel and (channel in self.channel_chats
                                 or channel in self.channel_threads))

    def send_notice(self, text: str, channel: Optional[str] = None) -> bool:
        """Send a plain status message (e.g. the end-of-scan summary). Goes to the
        channel's topic if given, else the group's General thread."""
        if not self.enabled:
            logger.info("Telegram disabled; notice: %s", text)
            return False
        payload = {"chat_id": self.chat_for(channel), "text": text,
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        thread = self.channel_threads.get(channel) if channel else None
        if thread:
            payload["message_thread_id"] = thread
        try:
            r = requests.post(TELEGRAM_API.format(token=self.bot_token), json=payload,
                              timeout=self.timeout)
            return r.status_code == 200
        except requests.RequestException as exc:
            logger.debug("Notice send error: %s", exc)
            return False

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def format_message(
        self,
        item: SourceItem,
        detection: DetectionResult,
        alert_score: Optional[float] = None,
    ) -> str:
        e = html.escape
        company = detection.company_name or "(unresolved)"
        ticker = detection.ticker or "?"

        # Header: direction + ticker + company + optional index tag.
        dir_emoji = {"bullish": "📈", "bearish": "📉"}.get(detection.direction, "🚨")
        header = f"{dir_emoji} <b>{e(ticker)}</b> — {e(company)}"
        if detection.in_index:
            header += f" <i>({e(detection.in_index)})</i>"

        # Status line: direction · confidence · score · verification verdict.
        status_parts = [f"<b>{e(detection.confidence.value)}</b> {e(detection.direction)}"]
        if alert_score is not None:
            status_parts.append(f"score {int(round(alert_score))}")
        status_parts.append(e(detection.verification_status or "n/a"))
        status = " · ".join(status_parts)

        lines = [header, status]

        # Matched phrase + trimmed excerpt.
        excerpt = detection.text_excerpt or ""
        if len(excerpt) > EXCERPT_CAP:
            excerpt = excerpt[: EXCERPT_CAP - 1].rstrip() + "…"
        if detection.matched_phrase:
            lines.append(f'💬 "{e(detection.matched_phrase)}": {e(excerpt)}')
        elif excerpt:
            lines.append(f"💬 {e(excerpt)}")

        # Time + source priority + source.
        when = _relative_time(item.timestamp) or e(item.timestamp or "")
        lines.append(
            f"🕒 {e(when)} · {e(detection.source_priority)} · {e(item.source)}"
        )

        # Link.
        lines.append(f"🔗 {e(item.url)}")

        # Ambiguity: other candidate tickers.
        candidates = [c for c in detection.candidate_tickers if c and c != ticker]
        if detection.ambiguous and candidates:
            lines.append(f"Candidates: {e(', '.join(candidates[:5]))}")

        # Social-rumor warning.
        warning = alert_policy.social_warning_for(detection)
        if warning:
            lines.append(f"<b>{e(warning)}</b>")

        lines.append("<i>Not financial advice.</i>")
        return "\n".join(lines)

    def send(
        self,
        item: SourceItem,
        detection: DetectionResult,
        detection_id: Optional[int] = None,
        alert_score: Optional[float] = None,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Send an alert. Returns (sent_ok, telegram_message_id, chat_id).

        When feedback is enabled and a detection_id is given, inline feedback
        buttons are attached so you can classify the alert from Telegram.
        """
        message = self.format_message(item, detection, alert_score=alert_score)
        target_chat = self.chat_for(item.channel)
        payload = {
            "chat_id": target_chat,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        # Route to a forum topic (sub-thread) within the group, if configured.
        thread = self.channel_threads.get(item.channel)
        if thread:
            payload["message_thread_id"] = thread
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
            chat_id = str(result.get("chat", {}).get("id")) if result.get("chat") else target_chat
            return (True, message_id, chat_id)
        except requests.RequestException as exc:
            logger.error("Telegram send error: %s", exc)
            return (False, None, None)
