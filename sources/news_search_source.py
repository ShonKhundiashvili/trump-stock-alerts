"""News-search adapter built on Google News RSS.

Google News publishes a public RSS endpoint for search queries:
    https://news.google.com/rss/search?q=<query>&hl=en-US&gl=US&ceid=US:en

We use it to run keyword query templates (e.g. "Trump says buy", "Trump Dell")
against mainstream news coverage. This is a SECONDARY source: it reports what
news outlets say Trump said, so on its own it tops out at MEDIUM confidence
(see alert_policy). It is compliant public RSS — no scraping, no bypass.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import List
from urllib.parse import quote_plus

from models import SourceItem

from .rss_source import RSSSource

logger = logging.getLogger(__name__)

# Google News titles are "Headline - Publisher". The trailing publisher is a
# common source of false ticker matches (e.g. "- MSN" -> MSN, "- Reuters" -> TRI),
# so we drop it.
_PUBLISHER_SUFFIX = re.compile(r"\s+-\s+[^-]{2,40}$")


def _strip_publisher(title: str) -> str:
    return _PUBLISHER_SUFFIX.sub("", title or "").strip()

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
)


def build_query_url(query: str) -> str:
    return GOOGLE_NEWS_RSS.format(query=quote_plus(query))


class NewsSearchSource(RSSSource):
    """A Google-News-RSS-backed keyword search source."""

    type = "news_search"

    def __init__(self, conn: sqlite3.Connection, query: str) -> None:
        url = build_query_url(query)
        # RSSSource names itself "rss:<name>"; override to "news_search:<query>".
        super().__init__(conn=conn, name=query, url=url)
        self.name = f"news_search:{query}"
        self.query = query

    def fetch_new_items(self) -> List[SourceItem]:
        items = super().fetch_new_items()
        # Use the clean headline (publisher stripped) as the classified text;
        # Google News summaries just repeat the headline + publisher.
        for it in items:
            headline = _strip_publisher(it.title or it.text)
            it.text = headline
            it.title = headline
        return items
