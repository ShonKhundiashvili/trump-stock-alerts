"""trump-stock-alerts — main polling loop.

Continuously:
  1. Loads config (sources, watchlist, phrases) and settings (.env).
  2. Builds enabled source adapters.
  3. Fetches new items from each source (failures isolated per source).
  4. Skips already-seen items.
  5. Runs the detector.
  6. Saves source item + detections in SQLite.
  7. Sends a Telegram alert when relevant (deduped per source-item + ticker).
  8. Marks alerts as sent.

This system CLASSIFIES information only. It does not auto-trade and does not give
financial advice. Every alert links to the original source for manual review.
"""

from __future__ import annotations

import logging
import signal
import time
from typing import List

import alert_policy
import config_loader
import db
from detector import Detector
from llm_classifier import LLMClassifier
from models import Confidence, DetectionResult, SourceItem
from sources import build_sources
from telegram_alerts import TelegramAlerter
from ticker_resolver import TickerResolver

logger = logging.getLogger("trump_stock_alerts")

_running = True


def _handle_signal(signum, _frame):
    global _running
    logger.info("Received signal %s; shutting down after current cycle.", signum)
    _running = False


def maybe_apply_llm(
    llm: LLMClassifier,
    text: str,
    detections: List[DetectionResult],
) -> None:
    """Optional second opinion. Only runs after rule-based detection found something.

    The LLM is NEVER the only detector and never gives advice. Here it can only
    annotate / upgrade-or-downgrade confidence of existing detections.
    """
    if not detections or not llm.enabled:
        return
    result = llm.classify(text)
    if result is None:
        return
    for det in detections:
        det.llm_used = True
        # Find a matching company by ticker or name.
        match = None
        for c in result.mentioned_companies:
            if (c.ticker and det.ticker and c.ticker.upper() == det.ticker.upper()) or (
                c.company_name and det.company_name
                and c.company_name.lower() in det.company_name.lower()
            ):
                match = c
                break
        if match:
            det.llm_reason = match.reason
        if not result.is_stock_related:
            det.llm_reason = (det.llm_reason or "") + " [LLM: not stock-related]"


def _keyword_ok(text: str, require_keywords: list) -> bool:
    if not require_keywords:
        return True
    low = (text or "").lower()
    return any(kw in low for kw in require_keywords)


def process_item(
    conn,
    item: SourceItem,
    detector: Detector,
    llm: LLMClassifier,
    alerter: TelegramAlerter,
    min_alert_rank: int = 2,
    require_keywords: list | None = None,
) -> None:
    # Stamp dedup keys before storage.
    item.canonical_url = alert_policy.canonicalize_url(item.url)
    item.text_hash = alert_policy.text_hash(item.text)

    rowid = db.insert_source_item(conn, item)
    if rowid is None:
        return  # already seen

    # Non-primary sources only get classified if they actually mention Trump.
    if not _keyword_ok(item.text, require_keywords or []):
        logger.debug("[%s] item lacks required keyword; stored, not classified.", item.source)
        return

    detections = detector.detect(item.text)
    if not detections:
        return

    maybe_apply_llm(llm, item.text, detections)

    for det in detections:
        # Stamp provenance BEFORE storing so corroboration sees the correct tier.
        det.source_priority = item.priority
        # Record the raw (text-confidence) detection first so corroboration can
        # see it across sources, then apply the provenance/verification policy.
        detection_id = db.insert_detection(conn, item, det, rowid)
        if det.confidence == Confidence.NONE:
            continue

        primary_found, secondary_count = db.corroboration(
            conn, det.ticker, alert_policy.CORROBORATION_WINDOW_HOURS
        )
        alert_policy.evaluate(det, item.priority, primary_found, secondary_count)
        db.update_detection_verdict(conn, detection_id, det.confidence.value,
                                    det.verification_status)

        # Everything is stored; only alert at/above the configured threshold.
        if det.confidence.rank() < min_alert_rank:
            logger.debug("Below alert threshold (%s, %s) for %s / %s; stored only.",
                         det.confidence.value, item.priority, item.source, det.ticker)
            continue

        # Dedup: same source-item+ticker, OR same statement text reposted elsewhere.
        if db.alert_already_sent(conn, item.source, item.source_item_id, det.ticker):
            continue
        if db.alert_sent_for_text_hash(conn, item.text_hash, det.ticker):
            logger.debug("Cross-source duplicate suppressed (%s) for %s", det.ticker, item.source)
            continue

        if not db.record_alert(conn, item.source, item.source_item_id, det.ticker,
                               detection_id, text_hash=item.text_hash):
            continue

        sent = alerter.send(item, det)
        if sent:
            db.mark_alert_sent(conn, detection_id)
            logger.info(
                "ALERT [%s|%s] %s (%s) conf=%s verify=%r via=%s",
                item.source, item.priority, det.company_name, det.ticker,
                det.confidence.value, det.verification_status, det.detected_via,
            )


def run_cycle(conn, settings, detector, llm, alerter) -> None:
    min_rank = Confidence(settings.min_alert_confidence).rank() \
        if settings.min_alert_confidence in Confidence.__members__ else Confidence.MEDIUM.rank()
    sources_config = config_loader.load_sources()
    sources = build_sources(sources_config, conn, settings)
    for source in sources:
        items = source.safe_fetch()
        for item in items:
            try:
                process_item(conn, item, detector, llm, alerter,
                             min_alert_rank=min_rank,
                             require_keywords=source.require_keywords)
            except Exception as exc:  # noqa: BLE001 - never crash the loop on one item
                logger.exception("Error processing item %s: %s", item.fingerprint(), exc)


def build_detector(settings) -> Detector:
    watchlist = config_loader.load_watchlist()
    phrases = config_loader.load_phrases()
    resolver = TickerResolver(watchlist=watchlist, enable_online=False)
    return Detector(resolver=resolver, phrases=phrases, watchlist=watchlist)


def main(run_once: bool = False) -> None:
    settings = config_loader.load_settings()
    config_loader.setup_logging(settings.log_level)

    mode = "single cycle (--once)" if run_once else f"loop every {settings.poll_seconds}s"
    logger.info("Starting trump-stock-alerts [%s]", mode)
    logger.info("Telegram enabled: %s | LLM enabled: %s | min_alert=%s",
                settings.telegram_enabled, settings.llm_enabled,
                settings.min_alert_confidence)
    if not settings.telegram_enabled:
        logger.warning("Telegram not configured — alerts will be logged, not sent.")

    conn = db.connect(settings.database_path)
    db.init_db(conn)

    detector = build_detector(settings)
    llm = LLMClassifier(
        openai_api_key=settings.openai_api_key,
        anthropic_api_key=settings.anthropic_api_key,
    )
    alerter = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id)

    if run_once:
        # One poll cycle then exit — used by scheduled runners (GitHub Actions/cron).
        try:
            run_cycle(conn, settings, detector, llm, alerter)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Cycle error: %s", exc)
        conn.close()  # checkpoints SQLite WAL into the main DB file before exit
        logger.info("Single cycle complete.")
        return

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while _running:
        try:
            run_cycle(conn, settings, detector, llm, alerter)
        except Exception as exc:  # noqa: BLE001 - keep the bot alive
            logger.exception("Cycle error (continuing): %s", exc)
        # Sleep in small steps so signals are handled promptly.
        for _ in range(settings.poll_seconds):
            if not _running:
                break
            time.sleep(1)

    conn.close()
    logger.info("Stopped.")


if __name__ == "__main__":
    import sys
    main(run_once="--once" in sys.argv)
