"""NewsAPI source — broad multi-outlet news search via the official NewsAPI.org.

Optional: needs NEWSAPI_KEY (free tier available). Each matching recent article
becomes a SECONDARY item, adding more independent outlets for cross-source
verification. Official API, no scraping.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests

import db
from models import SourceItem, now_iso
from .base import BaseSource

logger = logging.getLogger(__name__)

NEWSAPI_URL = "https://newsapi.org/v2/everything"


class NewsAPISource(BaseSource):
    type = "newsapi"

    def __init__(
        self,
        conn: sqlite3.Connection,
        name: str,
        query: str,
        api_key: Optional[str],
        lookback_hours: int = 24,
        page_size: int = 30,
        timeout: int = 20,
    ) -> None:
        super().__init__(conn, name=f"newsapi:{name}")
        self.display_name = name
        self.query = query
        self.api_key = api_key
        self.lookback_hours = lookback_hours
        self.page_size = max(1, min(page_size, 100))
        self.timeout = timeout

    def fetch_new_items(self) -> List[SourceItem]:
        if not self.api_key:
            logger.warning("[%s] NEWSAPI_KEY not set; skipping", self.name)
            return []
        since = (datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        params = {
            "q": self.query,
            "from": since,
            "sortBy": "publishedAt",
            "language": "en",
            "pageSize": self.page_size,
            "apiKey": self.api_key,
        }
        try:
            resp = requests.get(NEWSAPI_URL, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("[%s] NewsAPI error: %s", self.name, exc)
            return []
        if resp.status_code != 200:
            logger.warning("[%s] NewsAPI HTTP %s: %s", self.name, resp.status_code, resp.text[:200])
            return []

        items: List[SourceItem] = []
        for a in resp.json().get("articles", []) or []:
            url = a.get("url", "")
            if not url:
                continue
            item_id = hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()[:24]
            if db.source_item_exists(self.conn, self.name, item_id):
                continue
            title = a.get("title") or ""
            desc = a.get("description") or ""
            src = (a.get("source") or {}).get("name", "")
            items.append(SourceItem(
                source=self.name,
                source_item_id=item_id,
                url=url,
                text=" ".join(t for t in (title, desc) if t).strip(),
                timestamp=a.get("publishedAt") or now_iso(),
                title=title,
            ))
        self.touch()
        return items
