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
from alert_policy import assign_priority

from .base import BaseSource
from .news_search_source import NewsSearchSource
from .rss_source import RSSSource
from .truthsocial_source import TruthSocialSource
from .webpage_source import WebpageSource
from .x_source import XSource

logger = logging.getLogger(__name__)

__all__ = [
    "BaseSource",
    "RSSSource",
    "WebpageSource",
    "XSource",
    "TruthSocialSource",
    "NewsSearchSource",
    "build_sources",
]

# Default keyword filter applied to non-PRIMARY sources so general news/social
# feeds only classify items that actually mention Trump.
DEFAULT_TRUMP_KEYWORDS = ["trump", "realdonaldtrump", "potus", "president trump"]


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

    logger.info("Built %d source adapter(s): %s", len(sources),
                ", ".join(f"{s.name}[{s.priority}]" for s in sources))
    return sources
