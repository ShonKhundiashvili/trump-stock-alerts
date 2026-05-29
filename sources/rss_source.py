"""RSS / transcript feed adapter.

Monitors public RSS/Atom feeds. This is important because some market-moving
comments come from speeches and transcripts, not social posts.

It polls conservatively and respectfully and does NOT bypass any restrictions.
"""

from __future__ import annotations

import calendar
import hashlib
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import List

import db
from models import SourceItem
from .base import BaseSource

logger = logging.getLogger(__name__)

try:
    import feedparser
except Exception:  # pragma: no cover
    feedparser = None  # type: ignore

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None  # type: ignore


def _strip_html(text: str) -> str:
    """Strip HTML tags/links from feed content.

    Important: many feeds (e.g. Google News) embed source URLs in <a href>. Left
    in, a link like 'news.google.com' would falsely match the 'Google' alias.
    Stripping HTML removes those URLs and yields a clean, readable excerpt.
    """
    if not text:
        return ""
    if BeautifulSoup is None:
        return text
    return BeautifulSoup(text, "lxml").get_text(" ", strip=True)


def _entry_timestamp(entry) -> str:
    """Best-effort ISO8601 UTC timestamp for a feed entry.

    Prefers feedparser's parsed structs (published_parsed/updated_parsed, which
    are time.struct_time in UTC) and converts them to a reliable ISO8601 UTC
    string. Falls back to the raw published/updated string if no struct exists.
    """
    for attr in ("published_parsed", "updated_parsed"):
        st = getattr(entry, attr, None)
        if isinstance(st, time.struct_time):
            try:
                # feedparser normalizes parsed structs to UTC.
                dt = datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
                return dt.isoformat()
            except (ValueError, OverflowError):
                continue
    return getattr(entry, "published", "") or getattr(entry, "updated", "")


def _entry_id(entry) -> str:
    raw = getattr(entry, "id", None) or getattr(entry, "link", None) or getattr(entry, "title", "")
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:24]


class RSSSource(BaseSource):
    type = "rss"

    def __init__(self, conn: sqlite3.Connection, name: str, url: str) -> None:
        super().__init__(conn, name=f"rss:{name}")
        self.display_name = name
        self.url = url

    def fetch_new_items(self) -> List[SourceItem]:
        if feedparser is None:
            logger.warning("[%s] feedparser not installed; skipping", self.name)
            return []

        parsed = feedparser.parse(self.url)
        if getattr(parsed, "bozo", 0) and not parsed.entries:
            logger.warning("[%s] feed parse issue: %s", self.name, getattr(parsed, "bozo_exception", ""))
            self.touch()
            return []

        items: List[SourceItem] = []
        for entry in parsed.entries:
            item_id = _entry_id(entry)
            if db.source_item_exists(self.conn, self.name, item_id):
                continue
            title = _strip_html(getattr(entry, "title", "") or "")
            summary = _strip_html(
                getattr(entry, "summary", "") or getattr(entry, "description", "") or ""
            )
            content = ""
            if getattr(entry, "content", None):
                content = _strip_html(" ".join(c.get("value", "") for c in entry.content))
            text = " ".join(t for t in (title, summary, content) if t).strip()
            published = _entry_timestamp(entry)
            link = getattr(entry, "link", self.url)
            items.append(
                SourceItem(
                    source=self.name,
                    source_item_id=item_id,
                    url=link,
                    text=text,
                    timestamp=published,
                    title=title,
                )
            )
        self.touch()
        return items
