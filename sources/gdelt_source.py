"""GDELT source — free, keyless multi-outlet news corroboration.

GDELT's public Doc 2.0 API indexes worldwide news. We query it for recent
Trump + stock coverage; each article becomes a SECONDARY item. Because GDELT
aggregates many independent outlets, it strengthens cross-source verification
(more distinct sources reporting the same ticker → higher "CORROBORATED" score).

Official open API, no key, no scraping. We use a short timespan so only recent
articles are returned.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import List
from urllib.parse import urlparse

import requests

import db
from models import SourceItem, now_iso
from .base import BaseSource

logger = logging.getLogger(__name__)

GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"


class GDELTSource(BaseSource):
    type = "gdelt"

    def __init__(
        self,
        conn: sqlite3.Connection,
        name: str,
        query: str,
        timespan: str = "1d",
        max_records: int = 25,
        timeout: int = 25,
    ) -> None:
        super().__init__(conn, name=f"gdelt:{name}")
        self.display_name = name
        self.query = query
        self.timespan = timespan
        self.max_records = max(1, min(max_records, 75))
        self.timeout = timeout

    def fetch_new_items(self) -> List[SourceItem]:
        params = {
            "query": self.query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": self.max_records,
            "timespan": self.timespan,
            "sort": "DateDesc",
        }
        try:
            resp = requests.get(GDELT_API, params=params,
                                headers={"User-Agent": "trump-stock-alerts/1.0"},
                                timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("[%s] GDELT error: %s", self.name, exc)
            self.touch()
            return []
        if resp.status_code != 200:
            logger.warning("[%s] GDELT HTTP %s", self.name, resp.status_code)
            self.touch()
            return []
        try:
            articles = resp.json().get("articles", []) or []
        except ValueError:
            logger.debug("[%s] GDELT returned non-JSON", self.name)
            self.touch()
            return []

        items: List[SourceItem] = []
        for a in articles:
            url = a.get("url", "")
            if not url:
                continue
            item_id = hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()[:24]
            if db.source_item_exists(self.conn, self.name, item_id):
                continue
            title = a.get("title", "") or ""
            domain = a.get("domain") or urlparse(url).netloc
            # GDELT 'seendate' looks like 20260528T105916Z -> normalize to ISO.
            seen = a.get("seendate", "")
            ts = now_iso()
            if len(seen) >= 15 and seen[8] == "T":
                ts = f"{seen[0:4]}-{seen[4:6]}-{seen[6:8]}T{seen[9:11]}:{seen[11:13]}:{seen[13:15]}+00:00"
            items.append(SourceItem(
                source=self.name,
                source_item_id=item_id,
                url=url,
                text=f"{title} ({domain})".strip(),
                timestamp=ts,
                title=title,
            ))
        self.touch()
        return items
