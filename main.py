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
import threading
import time
from typing import List

import alert_policy
import config_loader
import db
import feedback_bot
import feedback_learning
import market_data
from detector import Detector
from llm_classifier import LLMClassifier
from models import Confidence, DetectionResult, SourceItem, SourcePriority
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


def should_alert(det: DetectionResult, min_alert_rank: int) -> bool:
    """Decide whether a detection is worth a Telegram alert.

    Normally: final confidence must meet the configured threshold.
    Exception: SOCIAL_RUMOR sources are capped at LOW by policy, so they'd never
    pass a MEDIUM threshold — but the rumor layer exists precisely to surface
    early-warning stock-calls. So we let a social item through (still labelled
    LOW + unverified) when its underlying TEXT actually contained a stock-call
    (text confidence MEDIUM/HIGH), while bare social company mentions stay quiet.
    """
    if det.confidence.rank() >= min_alert_rank:
        return True
    if det.source_priority == SourcePriority.SOCIAL_RUMOR.value:
        text_conf = det.text_confidence or det.confidence
        return text_conf.rank() >= Confidence.MEDIUM.rank()
    return False


_RELAY_LABELS = {
    "polymarket": "Prediction market (Polymarket)",
    "kalshi": "Prediction market (Kalshi)",
    "marketnews": "Market news",
    "ratings": "Analyst rating",
    "sec": "SEC stake filing (13D/13G)",
    "instnews": "Institutional news",
}


def _relay_label(source: str) -> str:
    return _RELAY_LABELS.get(source.split(":")[0], "Market signal")


def _enrich_trade_note(det: DetectionResult, settings, cache,
                       max_run_pct: float = 0.0) -> str:
    """Attach a price-reaction (too-late?) line + research trade plan to an alert,
    and decide whether the move already ran.

    Returns a suppression reason ("already_ran") when the signal is too late, else
    "". Best-effort: a data hiccup never blocks an alert (returns ""). Skipped when
    no ticker resolved or settings not provided (e.g. unit tests)."""
    if not det.ticker or settings is None:
        return ""
    try:
        q = market_data.quote(det.ticker, cache)
        if not q:
            return ""
        lines = []
        flag = market_data.too_late_flag(q, det.direction)
        if flag:
            lines.append(f"📊 ${q.price:g} · {flag}")
        plan = market_data.trade_plan(q, det.direction, settings.account_size,
                                      settings.risk_pct)
        if plan:
            lines.append(plan)
        det.trade_note = "\n".join(lines)
        if market_data.already_ran(q, det.direction, max_run_pct):
            return "already_ran"
        return ""
    except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
        logger.debug("Trade-note enrichment failed for %s: %s", det.ticker, exc)
        return ""


def process_relay(conn, item, detector, alerter, alerting, rowid, stats=None,
                  settings=None, quote_cache=None) -> None:
    """Forward a pre-filtered prediction-market item to its channel as news.

    Relay items (Polymarket/Kalshi) are already filtered to stock/crypto/M&A at
    the source, so they bypass the buy-phrase detector + corroboration gate. We
    still apply recency and dedup, opportunistically resolve a ticker for
    display, and route to the item's channel (e.g. 'predictions').
    """
    # Prediction markets are "standing" (hot when traded) so skip recency for
    # them; news-type relay (ratings / SEC / institutional news) must be recent.
    # Respect mutes (so the 🚫/🔕 buttons work on prediction/ratings/holder feeds).
    if alerting.get("respect_muted_sources", True) and db.is_source_muted(conn, item.source):
        return
    is_market = item.source.split(":")[0] in ("polymarket", "kalshi")
    max_age = 0 if is_market else alerting.get("relay_max_age_hours", 72)
    age = alert_policy.age_hours(item.timestamp)
    if max_age and age is not None and age > max_age:
        return  # too old

    # Resolve a ticker/company for display: (1) source-provided hint,
    # (2) detector on the text, (3) resolver on the title (clean company name).
    ticker = company = in_index = None
    if item.ticker:
        ticker = item.ticker
        cm = detector.resolver.resolve_ticker_token(item.ticker)
        if cm:
            company, in_index = cm.company_name, detector._index_label(item.ticker)
    if not ticker:
        try:
            dets = detector.detect(item.text)
        except Exception:  # noqa: BLE001
            dets = []
        if dets:
            best = max(dets, key=lambda d: d.confidence.rank())
            ticker, company, in_index = best.ticker, best.company_name, best.in_index
    if not ticker and item.title:
        cm = detector.resolver.resolve(item.title)
        if cm and cm.ticker and cm.resolution_confidence >= 90 and not cm.ambiguous:
            ticker, company = cm.ticker, cm.company_name
            in_index = detector._index_label(cm.ticker)

    det = DetectionResult(
        company_name=company or (item.title or item.text)[:80],
        ticker=ticker,
        candidate_tickers=[],
        confidence=Confidence.MEDIUM,
        ticker_resolution_confidence=0.0,
        matched_phrase=None,
        text_excerpt=item.text,
        detected_via="prediction-market",
        direction="neutral",
        in_index=in_index or "",
        source_priority=item.priority,
        verification_status=_relay_label(item.source),
    )
    detection_id = db.insert_detection(conn, item, det, rowid)
    db.set_alert_score(conn, detection_id, alerting.get("min_alert_score", 60))

    if alerting.get("respect_muted_companies", True) and db.is_company_muted(conn, det.ticker):
        db.set_alert_suppressed(conn, detection_id, "muted_company")
        return

    # Relay alerts require their OWN destination (a separate chat or a forum
    # topic) — never fall back to the main chat, so prediction markets can't
    # pollute the Trump/markets room.
    if not alerter.has_dedicated_route(item.channel):
        db.set_alert_suppressed(conn, detection_id, "no_channel")
        logger.debug("Relay item has no dedicated route; stored only: %s", item.source)
        return

    if db.alert_already_sent(conn, item.source, item.source_item_id, det.ticker):
        return
    if not db.record_alert(conn, item.source, item.source_item_id, det.ticker,
                           detection_id, text_hash=item.text_hash):
        return
    _enrich_trade_note(det, settings, quote_cache)
    sent, message_id, chat_id = alerter.send(item, det, detection_id=detection_id,
                                             alert_score=alerting.get("min_alert_score", 60))
    if sent:
        db.mark_alert_sent(conn, detection_id)
        db.update_alert_message(conn, detection_id, message_id, chat_id)
        if stats is not None:
            stats[item.channel] = stats.get(item.channel, 0) + 1
        logger.info("RELAY [%s|%s] %s", item.source, item.channel, (item.title or "")[:80])


def process_item(
    conn,
    item: SourceItem,
    detector: Detector,
    llm: LLMClassifier,
    alerter: TelegramAlerter,
    alerting: dict,
    require_keywords: list | None = None,
    stats: dict | None = None,
    settings=None,
    quote_cache: dict | None = None,
) -> None:
    # Stamp dedup keys before storage.
    item.canonical_url = alert_policy.canonicalize_url(item.url)
    item.text_hash = alert_policy.text_hash(item.text)

    rowid = db.insert_source_item(conn, item)
    if rowid is None:
        return  # already seen

    # Relay sources (prediction markets, ratings, SEC) are forwarded as-is.
    if item.relay:
        process_relay(conn, item, detector, alerter, alerting, rowid, stats,
                      settings=settings, quote_cache=quote_cache)
        return

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
        detection_id = db.insert_detection(conn, item, det, rowid)
        if det.confidence == Confidence.NONE:
            continue

        primary_found, secondary_count = db.corroboration(
            conn, det.ticker, alert_policy.CORROBORATION_WINDOW_HOURS
        )
        alert_policy.evaluate(det, item.priority, primary_found, secondary_count)
        db.update_detection_verdict(conn, detection_id, det.confidence.value,
                                    det.verification_status)

        # Score + gate (mutes, thresholds, learned adjustments). Everything is
        # stored; suppressed alerts keep a reason for transparency.
        decision = feedback_learning.evaluate_alert(conn, det, item, alerting)
        db.set_alert_score(conn, detection_id, decision.score)
        if not decision.send:
            db.set_alert_suppressed(conn, detection_id, decision.reason)
            logger.debug("Suppressed (%s, score=%s) %s / %s",
                         decision.reason, decision.score, item.source, det.ticker)
            continue

        # Freshness gate for the Trump room: secondary "aftermath" news (Google
        # News etc.) must be very recent so day-old/recirculated stories don't
        # masquerade as fresh calls. Trump's OWN primary statements keep the
        # normal recency window.
        trump_news_max = alerting.get("trump_news_max_age_hours", 18)
        if (item.channel == "trump" and item.priority != "PRIMARY" and trump_news_max):
            age = alert_policy.age_hours(item.timestamp)
            if age is not None and age > trump_news_max:
                db.set_alert_suppressed(conn, detection_id, "stale_news")
                logger.info("Suppressed (stale_news, %.0fh) %s / %s",
                            age, item.source, det.ticker)
                continue

        # Already-ran gate: don't push a buy/sell signal whose move already
        # happened (the opportunity is gone). Also attaches the price + plan note.
        ran = _enrich_trade_note(det, settings, quote_cache,
                                 max_run_pct=alerting.get("max_recent_run_pct", 12))
        if ran:
            db.set_alert_suppressed(conn, detection_id, ran)
            logger.info("Suppressed (already_ran) %s — move already happened", det.ticker)
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

        # det.trade_note was already populated by the already-ran gate above.
        sent, message_id, chat_id = alerter.send(
            item, det, detection_id=detection_id, alert_score=decision.score
        )
        if sent:
            db.mark_alert_sent(conn, detection_id)
            db.update_alert_message(conn, detection_id, message_id, chat_id)
            if stats is not None:
                stats[item.channel] = stats.get(item.channel, 0) + 1
            logger.info(
                "ALERT [%s|%s] %s (%s) conf=%s score=%s verify=%r",
                item.source, item.priority, det.company_name, det.ticker,
                det.confidence.value, decision.score, det.verification_status,
            )


def run_cycle(conn, settings, detector, llm, alerter) -> None:
    alerting = config_loader.load_alerting()
    sources_config = config_loader.load_sources()
    sources = build_sources(sources_config, conn, settings)
    stats: dict = {}
    quote_cache: dict = {}   # per-cycle price cache (one yfinance call per ticker)
    n_sources = n_errors = 0
    for source in sources:
        n_sources += 1
        items = source.safe_fetch()   # failures are isolated per source
        for item in items:
            try:
                process_item(conn, item, detector, llm, alerter, alerting,
                             require_keywords=source.require_keywords, stats=stats,
                             settings=settings, quote_cache=quote_cache)
            except Exception as exc:  # noqa: BLE001 - never crash the loop on one item
                n_errors += 1
                logger.exception("Error processing item %s: %s", item.fingerprint(), exc)

    total = sum(stats.values())
    logger.info("Cycle done: %d sources, %d alerts %s, %d item errors",
                n_sources, total, dict(stats), n_errors)
    # End-of-scan summary to the General topic (only when something fired).
    if total and alerting.get("cycle_summary", True):
        try:
            parts = ", ".join(f"{ch}: {n}" for ch, n in sorted(stats.items()))
            alerter.send_notice(f"✅ Scan complete — {total} new alert(s) → {parts}")
        except Exception as exc:  # noqa: BLE001 - summary must never crash the cycle
            logger.debug("Cycle summary send failed: %s", exc)


def _make_alerter(settings) -> TelegramAlerter:
    return TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id,
                           enable_feedback=settings.enable_feedback,
                           channel_chats=settings.channel_chats,
                           channel_threads=config_loader.load_topics())


def run_weekly_scan(settings) -> None:
    """Run the daily equity scanner and post results to the Scan topic.

    Also refreshes signal-performance outcomes (cheap, daily) so the
    /performance scorecard stays current and a one-line summary rides along.
    """
    import performance
    import scanner
    config_loader.setup_logging(settings.log_level)
    logger.info("Starting daily equity scan…")
    alerter = _make_alerter(settings)
    try:
        result = scanner.run_scan(settings, alerter)
        logger.info("Daily scan finished: %s", result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Daily scan failed: %s", exc)
    # Refresh price outcomes for past alerts + post the performance scorecard.
    try:
        conn = db.connect(settings.database_path)
        db.init_db(conn)
        performance.update_outcomes(conn)
        if alerter.enabled:
            channel = "weekly" if alerter.has_dedicated_route("weekly") else None
            alerter.send_notice(performance.summary(conn), channel=channel)
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Performance update failed: %s", exc)


def build_detector(settings) -> Detector:
    watchlist = config_loader.load_watchlist()
    phrases = config_loader.load_phrases()
    priority = config_loader.load_priority_tickers()
    resolver = TickerResolver(
        watchlist=watchlist, enable_online=False,
        index_tickers=set(priority.get("ALL", [])),
    )
    return Detector(resolver=resolver, phrases=phrases, watchlist=watchlist,
                    priority_tickers=priority)


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
    alerter = TelegramAlerter(settings.telegram_bot_token, settings.telegram_chat_id,
                              enable_feedback=settings.enable_feedback,
                              channel_chats=settings.channel_chats,
                              channel_threads=config_loader.load_topics())

    if run_once:
        # One poll cycle then exit — used by scheduled runners (GitHub Actions/cron).
        # Drain any feedback taps made since the last run first (offset persists
        # in the DB), then run one source cycle.
        if settings.enable_feedback and settings.telegram_enabled:
            try:
                feedback_bot.FeedbackBot(
                    settings.telegram_bot_token, settings.telegram_chat_id, conn,
                    extra_chat_ids=list(settings.channel_chats.values()),
                    settings=settings,
                ).drain()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Feedback drain error (continuing): %s", exc)
        try:
            run_cycle(conn, settings, detector, llm, alerter)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Cycle error: %s", exc)
        conn.close()  # checkpoints SQLite WAL into the main DB file before exit
        logger.info("Single cycle complete.")
        return

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Start the Telegram feedback receiver in its own thread (own DB connection).
    stop_event = threading.Event()
    feedback_thread = None
    if settings.enable_feedback and settings.telegram_enabled:
        feedback_thread = feedback_bot.start_in_thread(settings, stop_event)
        logger.info("Telegram feedback enabled (inline buttons + commands).")
    elif settings.enable_feedback:
        logger.warning("Feedback enabled but Telegram not configured; receiver not started.")

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

    stop_event.set()
    if feedback_thread:
        feedback_thread.join(timeout=5)
    conn.close()
    logger.info("Stopped.")


def run_perf_update(settings) -> None:
    """Standalone: refresh signal-performance outcomes (for a cron/manual run)."""
    import performance
    config_loader.setup_logging(settings.log_level)
    conn = db.connect(settings.database_path)
    db.init_db(conn)
    n = performance.update_outcomes(conn)
    logger.info("Performance update complete (%d outcomes). \n%s",
                n, performance.summary(conn))
    conn.close()


if __name__ == "__main__":
    import sys
    if "--scan" in sys.argv:
        run_weekly_scan(config_loader.load_settings())
    elif "--perf" in sys.argv:
        run_perf_update(config_loader.load_settings())
    else:
        main(run_once="--once" in sys.argv)
