"""Kalshi source — stock/crypto prediction-market headlines.

Uses Kalshi's public market-data API (keyless read; an optional KALSHI_API_KEY
is sent as a bearer token if provided). Keeps only stock/crypto/company/M&A
markets and emits them as RELAY items to the predictions channel. Official API,
no scraping.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import List, Optional

import requests

import db
from models import SourceItem, now_iso
from .base import BaseSource
from .prediction_filter import clean, is_market_relevant

logger = logging.getLogger(__name__)

API = "https://api.elections.kalshi.com/trade-api/v2/markets"
# Kalshi categories that are stock/crypto/economy relevant.
RELEVANT_CATEGORIES = {
    "financials", "financial", "economics", "economy", "crypto",
    "companies", "company", "science and technology", "technology",
}


class KalshiSource(BaseSource):
    type = "kalshi"

    def __init__(self, conn: sqlite3.Connection, name: str = "markets",
                 api_key: Optional[str] = None, max_markets: int = 200,
                 max_emit: int = 15, timeout: int = 25) -> None:
        super().__init__(conn, name=f"kalshi:{name}")
        self.api_key = api_key
        self.max_markets = max(20, min(max_markets, 1000))
        self.max_emit = max_emit
        self.timeout = timeout

    def _headers(self) -> dict:
        h = {"User-Agent": "trump-stock-alerts/1.0", "Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def fetch_new_items(self) -> List[SourceItem]:
        params = {"limit": min(self.max_markets, 200), "status": "open"}
        try:
            resp = requests.get(API, params=params, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("[%s] Kalshi error: %s", self.name, exc)
            self.touch()
            return []
        if resp.status_code != 200:
            logger.warning("[%s] Kalshi HTTP %s", self.name, resp.status_code)
            self.touch()
            return []
        try:
            markets = resp.json().get("markets", []) or []
        except ValueError:
            self.touch()
            return []

        items: List[SourceItem] = []
        for m in markets:
            title = clean(m.get("title", ""))
            ticker = str(m.get("ticker") or "")
            category = (m.get("category") or "").lower()
            if not title or not ticker:
                continue
            relevant = (category in RELEVANT_CATEGORIES) or is_market_relevant(title)
            if not relevant:
                continue
            if db.source_item_exists(self.conn, self.name, ticker):
                continue
            price = m.get("last_price") or m.get("yes_bid")
            prob = f"{price}%" if isinstance(price, (int, float)) else "?"
            text = f"{title} — Kalshi: {prob} yes"
            items.append(SourceItem(
                source=self.name,
                source_item_id=ticker,
                url=f"https://kalshi.com/markets/{ticker}",
                text=text,
                timestamp=m.get("open_time") or m.get("created_time") or now_iso(),
                title=title,
            ))
            if len(items) >= self.max_emit:
                break
        self.touch()
        return items
