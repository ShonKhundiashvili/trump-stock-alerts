"""Human-in-the-loop Telegram feedback receiver.

Long-polls the Telegram Bot API (getUpdates) for:
  - callback_query events from the inline feedback buttons on alerts, and
  - text commands (/stats, /mutes, /unmute_source, /unmute_company, /recent, /help).

It stores feedback in SQLite and never trades, never gives advice, and never
edits code/config. Security: only the configured TELEGRAM_CHAT_ID is honoured;
everything else is ignored.

Runs in its own thread alongside the source-polling loop (see main.py). It has
its own SQLite connection and is wrapped so a crash is logged and restarted
without taking down source polling.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional, Tuple

import requests

import db
import feedback_learning

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
OFFSET_KEY = "telegram_offset"

# action -> confirmation text shown back to the user.
LABEL_TEXT = {
    "useful": "✅ Useful / Real Signal",
    "fake": "❌ Fake / Wrong",
    "not_useful": "⚠️ Real but Not Useful",
    "needs_context": "🧵 Needs More Context",
    "mute_source": "🚫 Mute This Source",
    "mute_company": "🔕 Mute This Company",
    "too_late": "📈 Too Late",
    "training": "🧪 Mark as Training Example",
}
VALID_ACTIONS = set(LABEL_TEXT)


def parse_callback_data(data: str) -> Optional[Tuple[int, str]]:
    """Parse 'feedback:<detection_id>:<action>' -> (detection_id, action) or None."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "feedback":
        return None
    try:
        detection_id = int(parts[1])
    except ValueError:
        return None
    action = parts[2]
    if action not in VALID_ACTIONS:
        return None
    return detection_id, action


class FeedbackBot:
    def __init__(self, bot_token: str, chat_id: str, conn, timeout: int = 25,
                 extra_chat_ids=None, settings=None) -> None:
        self.bot_token = bot_token
        self.primary_chat_id = str(chat_id)
        # All chats allowed to give feedback/commands (default + per-channel chats).
        self.authorized = {str(chat_id)} | {str(c) for c in (extra_chat_ids or []) if c}
        self.conn = conn
        self.timeout = timeout
        self.settings = settings   # enables /scan (weekly equity scanner)

    # -- low-level API -------------------------------------------------- #
    def _api(self, method: str, payload: dict, timeout: Optional[int] = None) -> Optional[dict]:
        try:
            resp = requests.post(
                API.format(token=self.bot_token, method=method),
                json=payload,
                timeout=timeout or (self.timeout + 10),
            )
            if resp.status_code != 200:
                logger.debug("Telegram %s -> %s: %s", method, resp.status_code, resp.text[:200])
                return None
            return resp.json()
        except requests.RequestException as exc:
            logger.debug("Telegram %s error: %s", method, exc)
            return None

    def _answer_callback(self, callback_id: str, text: str = "") -> None:
        self._api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})

    def _send(self, text: str, chat_id=None) -> None:
        self._api("sendMessage", {
            "chat_id": chat_id or self.primary_chat_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        })

    def _show_saved(self, chat_id, message_id, action: str) -> None:
        """Replace the buttons with a single confirmation row (best-effort)."""
        markup = {"inline_keyboard": [[{
            "text": f"✔ Saved: {LABEL_TEXT.get(action, action)}",
            "callback_data": "noop",
        }]]}
        self._api("editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id, "reply_markup": markup,
        })

    # -- authorization -------------------------------------------------- #
    def _authorized(self, chat_id) -> bool:
        return str(chat_id) in self.authorized

    # -- callback handling ---------------------------------------------- #
    def handle_callback(self, cq: dict) -> None:
        callback_id = cq.get("id")
        message = cq.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        from_user = cq.get("from") or {}

        # Security: ignore anything not from the configured chat.
        if not self._authorized(chat_id):
            if callback_id:
                self._answer_callback(callback_id, "Not authorized.")
            logger.warning("Ignored callback from unauthorized chat %s", chat_id)
            return

        parsed = parse_callback_data(cq.get("data", ""))
        if not parsed:
            if callback_id:
                self._answer_callback(callback_id, "Unrecognized.")
            return
        detection_id, action = parsed

        det = db.get_detection(self.conn, detection_id)
        if det is None:
            # Old/expired alert — acknowledge gracefully, don't crash.
            if callback_id:
                self._answer_callback(callback_id, "This alert is no longer available.")
            return

        # Store the feedback.
        db.insert_feedback(
            self.conn,
            detection_id=detection_id,
            source_item_id=det["source_item_id"],
            ticker=det["ticker"],
            company_name=det["company"],
            source=det["source"],
            source_priority=det["source_priority"],
            original_confidence=det["confidence"],
            feedback_label=action,
            telegram_message_id=str(message.get("message_id")),
            telegram_chat_id=str(chat_id),
            user_id=str(from_user.get("id")),
            username=from_user.get("username"),
            comment=None,
        )

        # Side effects for specific actions.
        if action == "mute_source":
            db.mute_source(self.conn, det["source"], reason="muted via Telegram")
        elif action == "mute_company" and det["ticker"]:
            db.mute_company(self.conn, det["ticker"], det["company"] or "",
                            reason="muted via Telegram")
        elif action == "training":
            features = {
                "confidence": det["confidence"],
                "text_confidence": det["text_confidence"],
                "source_priority": det["source_priority"],
                "matched_phrase": det["matched_phrase"],
                "verification_status": det["verification_status"],
                "alert_score": det["alert_score"],
            }
            db.insert_training_example(
                self.conn,
                detection_id=detection_id,
                text=det["text"],
                source=det["source"],
                url=det["url"],
                ticker=det["ticker"],
                company_name=det["company"],
                model_features_json=json.dumps(features),
                user_label="training",
            )

        # Acknowledge + reflect in the message.
        if callback_id:
            self._answer_callback(callback_id, f"Feedback saved: {LABEL_TEXT.get(action, action)}")
        self._show_saved(chat_id, message.get("message_id"), action)
        logger.info("Feedback %s for detection %s (%s)", action, detection_id, det["ticker"])

    # -- command handling ----------------------------------------------- #
    def handle_command(self, message: dict) -> None:
        chat_id = (message.get("chat") or {}).get("id")
        if not self._authorized(chat_id):
            logger.warning("Ignored command from unauthorized chat %s", chat_id)
            return
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        arg = " ".join(parts[1:]).strip()

        if cmd == "/help":
            self._send(self._help_text(), chat_id)
        elif cmd == "/stats":
            self._send(self._stats_text(), chat_id)
        elif cmd == "/mutes":
            self._send(self._mutes_text(), chat_id)
        elif cmd == "/recent":
            self._send(self._recent_text(), chat_id)
        elif cmd == "/scan":
            self._start_scan(chat_id)
        elif cmd == "/unmute_source":
            if not arg:
                self._send("Usage: /unmute_source &lt;source&gt;", chat_id)
            else:
                ok = db.unmute_source(self.conn, arg)
                self._send(f"{'Unmuted' if ok else 'Not muted'} source: {arg}", chat_id)
        elif cmd == "/unmute_company":
            if not arg:
                self._send("Usage: /unmute_company &lt;ticker&gt;", chat_id)
            else:
                ok = db.unmute_company(self.conn, arg.upper())
                self._send(f"{'Unmuted' if ok else 'Not muted'} company: {arg.upper()}", chat_id)
        else:
            self._send("Unknown command. Try /help", chat_id)

    def _start_scan(self, chat_id) -> None:
        """Kick off the weekly equity scan in a background thread (best on a
        continuous host; on the scheduled --once runner use the weekly workflow)."""
        if not self.settings:
            self._send("Scan unavailable here — it runs on the weekly schedule.", chat_id)
            return
        self._send("📊 Running equity scan now — results will post to the Weekly "
                   "Scan topic in a few minutes. (Research only, not advice.)", chat_id)

        def _run():
            try:
                import config_loader
                import scanner
                from telegram_alerts import TelegramAlerter
                alerter = TelegramAlerter(
                    self.settings.telegram_bot_token, self.settings.telegram_chat_id,
                    channel_chats=self.settings.channel_chats,
                    channel_threads=config_loader.load_topics())
                scanner.run_scan(self.settings, alerter)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Manual /scan failed: %s", exc)

        threading.Thread(target=_run, name="manual-scan", daemon=True).start()

    # -- command text builders ------------------------------------------ #
    @staticmethod
    def _help_text() -> str:
        return (
            "<b>Commands</b>\n"
            "/stats - feedback &amp; source/ticker stats\n"
            "/mutes - list muted sources &amp; companies\n"
            "/unmute_source &lt;source&gt; - unmute a source\n"
            "/unmute_company &lt;ticker&gt; - unmute a ticker\n"
            "/recent - last 5 alerts &amp; their feedback\n"
            "/scan - run the weekly equity scan now (research only)\n"
            "/help - this message\n\n"
            "Tap the buttons under an alert to classify it. Learning is local, "
            "rule-based, and transparent. Not financial advice."
        )

    def _stats_text(self) -> str:
        c = self.conn
        total_alerts = c.execute(
            "SELECT COUNT(*) FROM detections WHERE alert_sent = 1"
        ).fetchone()[0]
        counts = db.overall_feedback_counts(c)
        top_src = c.execute("""
            SELECT source, COUNT(*) n,
              SUM(CASE feedback_label WHEN 'useful' THEN 1 ELSE 0 END) useful
            FROM feedback GROUP BY source ORDER BY useful DESC, n DESC LIMIT 5
        """).fetchall()
        worst_src = c.execute("""
            SELECT source,
              SUM(CASE WHEN feedback_label IN ('fake','not_useful') THEN 1 ELSE 0 END) bad
            FROM feedback GROUP BY source HAVING bad > 0 ORDER BY bad DESC LIMIT 5
        """).fetchall()
        top_tick = c.execute("""
            SELECT ticker,
              SUM(CASE feedback_label WHEN 'useful' THEN 1 ELSE 0 END) useful
            FROM feedback WHERE ticker IS NOT NULL
            GROUP BY ticker ORDER BY useful DESC LIMIT 5
        """).fetchall()
        muted_s = db.list_muted_sources(c)
        muted_c = db.list_muted_companies(c)

        def fmt(rows, a, b):
            return ", ".join(f"{r[a]}({r[b]})" for r in rows if r[a]) or "—"

        return (
            "<b>📊 Stats</b>\n"
            f"Alerts sent: {total_alerts}\n"
            f"Useful: {counts.get('useful', 0)} | Fake: {counts.get('fake', 0)} | "
            f"Not useful: {counts.get('not_useful', 0)} | "
            f"Needs context: {counts.get('needs_context', 0)} | "
            f"Too late: {counts.get('too_late', 0)}\n\n"
            f"<b>Top useful sources:</b> {fmt(top_src, 'source', 'useful')}\n"
            f"<b>Worst sources:</b> {fmt(worst_src, 'source', 'bad')}\n"
            f"<b>Top useful tickers:</b> {fmt(top_tick, 'ticker', 'useful')}\n\n"
            f"<b>Muted sources ({len(muted_s)}):</b> "
            f"{', '.join(r['source'] for r in muted_s) or '—'}\n"
            f"<b>Muted companies ({len(muted_c)}):</b> "
            f"{', '.join(r['ticker'] for r in muted_c) or '—'}"
        )

    def _mutes_text(self) -> str:
        s = db.list_muted_sources(self.conn)
        cmp = db.list_muted_companies(self.conn)
        return (
            "<b>🔇 Muted</b>\n"
            f"<b>Sources:</b> {', '.join(r['source'] for r in s) or '—'}\n"
            f"<b>Companies:</b> {', '.join(r['ticker'] for r in cmp) or '—'}\n\n"
            "Unmute with /unmute_source &lt;source&gt; or /unmute_company &lt;ticker&gt;"
        )

    def _recent_text(self) -> str:
        rows = db.recent_detections(self.conn, limit=5)
        if not rows:
            return "No recent detections."
        out = ["<b>🕒 Recent alerts</b>"]
        for r in rows:
            fb = db.feedback_for_detection(self.conn, r["id"])
            labels = ", ".join(f["feedback_label"] for f in fb) or "no feedback"
            sent = "sent" if r["alert_sent"] else f"suppressed:{r['alert_suppressed_reason'] or '?'}"
            out.append(
                f"#{r['id']} {r['ticker'] or '?'} [{r['confidence']}/{r['alert_score']}] "
                f"{sent} — {labels}"
            )
        return "\n".join(out)

    # -- main loop ------------------------------------------------------- #
    def _get_offset(self) -> Optional[int]:
        raw = db.get_last_seen_id(self.conn, OFFSET_KEY)
        return int(raw) if raw else None

    def _set_offset(self, offset: int) -> None:
        db.set_source_state(self.conn, OFFSET_KEY, last_seen_id=str(offset))

    def handle_update(self, update: dict) -> None:
        try:
            if "callback_query" in update:
                self.handle_callback(update["callback_query"])
            elif "message" in update and (update["message"].get("text") or "").startswith("/"):
                self.handle_command(update["message"])
        except Exception as exc:  # noqa: BLE001 - never let one update kill the loop
            logger.exception("Error handling update: %s", exc)

    def poll_once(self, long_poll_timeout: Optional[int] = None) -> int:
        offset = self._get_offset()
        timeout = self.timeout if long_poll_timeout is None else long_poll_timeout
        payload = {"timeout": timeout, "allowed_updates": ["callback_query", "message"]}
        if offset is not None:
            payload["offset"] = offset
        data = self._api("getUpdates", payload, timeout=timeout + 10)
        if not data or not data.get("ok"):
            return 0
        updates = data.get("result", [])
        for update in updates:
            self.handle_update(update)
            self._set_offset(update["update_id"] + 1)
        return len(updates)

    def drain(self, max_batches: int = 20) -> int:
        """Process all currently-pending updates without long-polling.

        Used by the scheduled (--once) runner: button taps made between runs are
        picked up here on the next run (the offset is persisted in the DB).
        """
        total = 0
        for _ in range(max_batches):
            n = self.poll_once(long_poll_timeout=0)
            total += n
            if n == 0:
                break
        if total:
            logger.info("Drained %d feedback update(s).", total)
        return total

    def run_forever(self, stop_event: Optional[threading.Event] = None) -> None:
        logger.info("Feedback bot started (long polling).")
        while stop_event is None or not stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Feedback poll error (continuing): %s", exc)
                time.sleep(5)


def start_in_thread(settings, stop_event: threading.Event) -> Optional[threading.Thread]:
    """Spawn the feedback bot in a daemon thread with its own DB connection."""
    if not settings.telegram_enabled:
        logger.warning("Feedback bot not started: Telegram not configured.")
        return None

    def _runner():
        conn = db.connect(settings.database_path)
        db.init_db(conn)
        bot = FeedbackBot(settings.telegram_bot_token, settings.telegram_chat_id, conn,
                          extra_chat_ids=list(settings.channel_chats.values()),
                          settings=settings)
        while not stop_event.is_set():
            try:
                bot.run_forever(stop_event)
            except Exception as exc:  # noqa: BLE001 - restart on unexpected crash
                logger.exception("Feedback bot crashed; restarting in 10s: %s", exc)
                if stop_event.wait(10):
                    break

    thread = threading.Thread(target=_runner, name="feedback-bot", daemon=True)
    thread.start()
    return thread
