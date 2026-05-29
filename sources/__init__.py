"""Modular source adapters.

Each adapter implements BaseSource and yields normalized SourceItem objects.
Add new sources by subclassing BaseSource and registering them in build_sources.

Every source is tagged with a provenance priority (PRIMARY / SECONDARY /
SOCIAL_RUMOR) from config/source_priority.json, which alert_policy.py uses to
decide final alert confidence and verification status.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List

import config_loader
from alert_policy import assign_channel, assign_priority

from .base import BaseSource
from .gdelt_source import GDELTSource
from .kalshi_source import KalshiSource
from .news_search_source import NewsSearchSource
from .newsapi_source import NewsAPISource
from .polymarket_source import PolymarketSource
from .ratings_source import RatingsSource
from .rss_source import RSSSource
from .sec_stakes_source import SECStakesSource
from .truthsocial_source import TruthSocialSource
from .usaspending_source import USASpendingSource
from .webpage_source import WebpageSource
from .x_source import XSource
from .youtube_source import YouTubeSource

logger = logging.getLogger(__name__)

__all__ = [
    "BaseSource",
    "RSSSource",
    "WebpageSource",
    "XSource",
    "TruthSocialSource",
    "NewsSearchSource",
    "GDELTSource",
    "NewsAPISource",
    "YouTubeSource",
    "build_sources",
]

# Default keyword filter applied to non-PRIMARY sources so general news/social
# feeds only classify items about the market-moving figures we track (Trump +
# administration + a few other high-impact speakers).
DEFAULT_TRUMP_KEYWORDS = [
    "trump", "realdonaldtrump", "potus", "president trump", "white house",
    "treasury", "bessent", "powell", "the fed", "federal reserve",
    "commerce secretary", "musk", "elon", "administration",
]


def _finalize(src: BaseSource, priority_cfg: Dict, explicit_priority=None,
              require_keywords=None) -> BaseSource:
    src.priority = (explicit_priority or assign_priority(src.name, priority_cfg)).upper()
    if require_keywords is not None:
        src.require_keywords = [k.lower() for k in require_keywords]
    elif src.priority != "PRIMARY":
        # Non-primary feeds are not Trump-specific; require a Trump keyword.
        src.require_keywords = DEFAULT_TRUMP_KEYWORDS
    return src


def build_sources(
    sources_config: Dict[str, Any],
    conn: sqlite3.Connection,
    settings: Any,
) -> List[BaseSource]:
    """Instantiate enabled source adapters from config/sources.json."""
    priority_cfg = config_loader.load_source_priority()
    channels_cfg = config_loader.load_channels()
    sources: List[BaseSource] = []

    # --- X / Twitter (official API) ------------------------------------- #
    x_cfg = sources_config.get("x", {})
    if x_cfg.get("enabled"):
        for account in x_cfg.get("accounts", ["realDonaldTrump"]):
            sources.append(_finalize(
                XSource(conn=conn, account=account,
                        bearer_token=settings.x_bearer_token,
                        max_results=x_cfg.get("max_results", 10)),
                priority_cfg))

    # --- RSS / transcript feeds (PRIMARY + SECONDARY news) -------------- #
    rss_cfg = sources_config.get("rss", {})
    if rss_cfg.get("enabled"):
        for feed in rss_cfg.get("feeds", []):
            sources.append(_finalize(
                RSSSource(conn=conn, name=feed["name"], url=feed["url"]),
                priority_cfg,
                explicit_priority=feed.get("priority"),
                require_keywords=feed.get("require_keywords"),
            ))

    # --- Keyword news search (Google News RSS) — SECONDARY -------------- #
    ns_cfg = sources_config.get("news_search", {})
    if ns_cfg.get("enabled"):
        for query in ns_cfg.get("queries", []):
            sources.append(_finalize(
                NewsSearchSource(conn=conn, query=query),
                priority_cfg,
                explicit_priority=ns_cfg.get("priority", "SECONDARY"),
                require_keywords=ns_cfg.get("require_keywords"),
            ))

    # --- Reddit / social chatter (RSS) — SOCIAL_RUMOR ------------------- #
    reddit_cfg = sources_config.get("reddit", {})
    if reddit_cfg.get("enabled"):
        for feed in reddit_cfg.get("feeds", []):
            src = RSSSource(conn=conn, name=feed["name"], url=feed["url"])
            src.name = f"reddit:{feed['name']}"
            sources.append(_finalize(
                src, priority_cfg,
                explicit_priority=feed.get("priority", "SOCIAL_RUMOR"),
                require_keywords=feed.get("require_keywords"),
            ))

    # --- YouTube (official Data API + optional captions) --------------- #
    yt_cfg = sources_config.get("youtube", {})
    if yt_cfg.get("enabled") and settings.youtube_api_key:
        for ch in yt_cfg.get("channels", []):
            sources.append(_finalize(
                YouTubeSource(
                    conn=conn, name=ch["name"], api_key=settings.youtube_api_key,
                    channel_id=ch.get("channel_id"), handle=ch.get("handle"),
                    fetch_transcripts=yt_cfg.get("fetch_transcripts", True),
                    max_videos=yt_cfg.get("max_videos", 10),
                ),
                priority_cfg,
                explicit_priority=ch.get("priority"),
                require_keywords=ch.get("require_keywords"),
            ))

    # --- GDELT (free, keyless multi-outlet corroboration) — SECONDARY --- #
    gd_cfg = sources_config.get("gdelt", {})
    if gd_cfg.get("enabled"):
        for q in gd_cfg.get("queries", []):
            name = q if isinstance(q, str) else q.get("name", q.get("query", "q"))
            query = q if isinstance(q, str) else q.get("query")
            sources.append(_finalize(
                GDELTSource(conn=conn, name=name, query=query,
                            timespan=gd_cfg.get("timespan", "1d"),
                            max_records=gd_cfg.get("max_records", 25)),
                priority_cfg,
                explicit_priority=gd_cfg.get("priority", "SECONDARY"),
                require_keywords=gd_cfg.get("require_keywords"),
            ))

    # --- NewsAPI (broad multi-outlet search, needs key) — SECONDARY ----- #
    na_cfg = sources_config.get("newsapi", {})
    if na_cfg.get("enabled") and settings.newsapi_key:
        for q in na_cfg.get("queries", []):
            sources.append(_finalize(
                NewsAPISource(conn=conn, name=q, query=q,
                              api_key=settings.newsapi_key,
                              lookback_hours=na_cfg.get("lookback_hours", 24)),
                priority_cfg,
                explicit_priority=na_cfg.get("priority", "SECONDARY"),
                require_keywords=na_cfg.get("require_keywords"),
            ))

    # --- USAspending federal contracts (official .gov API) — PRIMARY --- #
    us_cfg = sources_config.get("usaspending", {})
    if us_cfg.get("enabled"):
        sources.append(_finalize(
            USASpendingSource(
                conn=conn, name=us_cfg.get("name", "federal contracts"),
                min_amount=us_cfg.get("min_amount", 50_000_000),
                lookback_days=us_cfg.get("lookback_days", 7),
                max_records=us_cfg.get("max_records", 25),
            ),
            priority_cfg,
            explicit_priority=us_cfg.get("priority", "PRIMARY"),
            require_keywords=us_cfg.get("require_keywords", []),  # company IS the subject
        ))

    # --- Prediction markets (Polymarket / Kalshi) — relay to predictions #
    pm_cfg = sources_config.get("polymarket", {})
    if pm_cfg.get("enabled"):
        src = PolymarketSource(conn=conn, max_markets=pm_cfg.get("max_markets", 100))
        src.relay = True
        sources.append(_finalize(src, priority_cfg,
                                 explicit_priority=pm_cfg.get("priority", "PRIMARY"),
                                 require_keywords=[]))

    ks_cfg = sources_config.get("kalshi", {})
    if ks_cfg.get("enabled"):
        src = KalshiSource(conn=conn, api_key=settings.kalshi_api_key,
                           max_markets=ks_cfg.get("max_markets", 200))
        src.relay = True
        sources.append(_finalize(src, priority_cfg,
                                 explicit_priority=ks_cfg.get("priority", "PRIMARY"),
                                 require_keywords=[]))

    # --- Analyst ratings (FMP, needs key) — relay to ratings ----------- #
    rt_cfg = sources_config.get("ratings", {})
    if rt_cfg.get("enabled") and settings.fmp_api_key:
        src = RatingsSource(conn=conn, api_key=settings.fmp_api_key,
                            pages=rt_cfg.get("pages", 1))
        src.relay = True
        sources.append(_finalize(src, priority_cfg,
                                 explicit_priority=rt_cfg.get("priority", "PRIMARY"),
                                 require_keywords=[]))

    # --- SEC 13D/13G stake filings — relay to institutions ------------- #
    sec_cfg = sources_config.get("sec_stakes", {})
    if sec_cfg.get("enabled"):
        src = SECStakesSource(conn=conn, max_emit=sec_cfg.get("max_emit", 12))
        src.relay = True
        sources.append(_finalize(src, priority_cfg,
                                 explicit_priority=sec_cfg.get("priority", "PRIMARY"),
                                 require_keywords=[]))

    # --- Generic public webpages / transcripts (PRIMARY) --------------- #
    web_cfg = sources_config.get("webpages", {})
    if web_cfg.get("enabled"):
        for page in web_cfg.get("pages", []):
            sources.append(_finalize(
                WebpageSource(conn=conn, name=page["name"], url=page["url"],
                              selector=page.get("selector"),
                              min_interval_seconds=page.get("min_interval_seconds", 1800)),
                priority_cfg,
                explicit_priority=page.get("priority"),
                require_keywords=page.get("require_keywords"),
            ))

    # --- Truth Social placeholder (PRIMARY when compliant feed given) --- #
    ts_cfg = sources_config.get("truthsocial", {})
    if ts_cfg.get("enabled"):
        for account in ts_cfg.get("accounts", ["realDonaldTrump"]):
            sources.append(_finalize(
                TruthSocialSource(conn=conn, account=account,
                                  rss_url=ts_cfg.get("rss_url")),
                priority_cfg))

    # Route each source to its Telegram channel bucket.
    for s in sources:
        s.channel = assign_channel(s.name, channels_cfg)

    logger.info("Built %d source adapter(s): %s", len(sources),
                ", ".join(f"{s.name}[{s.priority}/{s.channel}]" for s in sources))
    return sources
