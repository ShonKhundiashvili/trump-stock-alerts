"""Analyst-ratings source — structured upgrades/downgrades & price targets.

Uses Financial Modeling Prep (FMP) — needs a free FMP_API_KEY. Pulls the
market-wide rating-changes feed (firm, previous→new grade, ticker, price target)
from firms like Morgan Stanley, JPMorgan, BofA, Piper Sandler, etc. Each change is
relayed to the ratings channel with the ticker set, so it lands in the Ratings
topic. Skipped automatically when no key is configured.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import List, Optional

import requests

import db
from models import SourceItem, now_iso
from .base import BaseSource

logger = logging.getLogger(__name__)

# FMP's "stable" API replaced the legacy /api/v3/upgrades-downgrades-rss-feed
# (now 403 for accounts created after 2025-08-31). grades-latest-news is the
# market-wide rating-changes feed and carries the same fields we parse below.
FMP_FEED = "https://financialmodelingprep.com/stable/grades-latest-news"


class RatingsSource(BaseSource):
    type = "ratings"

    def __init__(self, conn: sqlite3.Connection, name: str = "fmp",
                 api_key: Optional[str] = None, pages: int = 1,
                 max_emit: int = 20, timeout: int = 20) -> None:
        super().__init__(conn, name=f"ratings:{name}")
        self.api_key = api_key
        self.pages = pages
        self.max_emit = max_emit
        self.timeout = timeout

    def fetch_new_items(self) -> List[SourceItem]:
        if not self.api_key:
            logger.warning("[%s] FMP_API_KEY not set; skipping", self.name)
            return []
        items: List[SourceItem] = []
        for page in range(self.pages):
            try:
                resp = requests.get(FMP_FEED, params={"page": page, "apikey": self.api_key},
                                    timeout=self.timeout)
            except requests.RequestException as exc:
                logger.warning("[%s] FMP error: %s", self.name, exc)
                break
            if resp.status_code != 200:
                logger.warning("[%s] FMP HTTP %s: %s", self.name, resp.status_code, resp.text[:160])
                break
            try:
                rows = resp.json()
            except ValueError:
                break
            for r in rows or []:
                symbol = (r.get("symbol") or "").upper()
                firm = r.get("gradingCompany") or r.get("newsPublisher") or "Analyst"
                new_grade = r.get("newGrade") or ""
                prev_grade = r.get("previousGrade") or ""
                action = r.get("action") or ""
                target = r.get("priceTarget") or r.get("priceWhenPosted")
                published = r.get("publishedDate") or now_iso()
                if not symbol or not new_grade:
                    continue
                item_id = f"{symbol}:{firm}:{new_grade}:{published[:10]}"
                if db.source_item_exists(self.conn, self.name, item_id):
                    continue
                change = f"{prev_grade} → {new_grade}" if prev_grade else new_grade
                pt = f", PT {target}" if target else ""
                items.append(SourceItem(
                    source=self.name,
                    source_item_id=item_id,
                    url=r.get("newsURL") or f"https://www.google.com/search?q={symbol}+{firm}+rating",
                    text=f"{firm} {action or 'rates'} {symbol}: {change}{pt}",
                    timestamp=published,
                    title=symbol,
                    ticker=symbol,
                ))
                if len(items) >= self.max_emit:
                    return items
        self.touch()
        return items
