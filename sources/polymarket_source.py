"""Polymarket source — stock/crypto prediction-market headlines.

Uses Polymarket's public Gamma API (keyless) to pull recent active markets,
keeps only stock/crypto/company/M&A ones (see prediction_filter), and emits each
as a RELAY item routed to the predictions channel. Relay items are forwarded as
informational news (with the current odds) rather than run through the buy-signal
detector. Compliant public API, no scraping.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import List

import requests

import db
from models import SourceItem, now_iso
from .base import BaseSource
from .prediction_filter import clean, is_market_relevant, lead_probability, yes_probability

logger = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com/markets"


class PolymarketSource(BaseSource):
    type = "polymarket"

    def __init__(self, conn: sqlite3.Connection, name: str = "markets",
                 max_markets: int = 200, max_emit: int = 15,
                 min_volume: float = 50_000, min_probability: float = 0.50,
                 timeout: int = 25) -> None:
        super().__init__(conn, name=f"polymarket:{name}")
        self.max_markets = max(10, min(max_markets, 500))
        self.max_emit = max_emit  # cap new relay items per cycle (avoid flooding)
        self.min_volume = min_volume
        self.min_probability = min_probability
        self.timeout = timeout

    def fetch_new_items(self) -> List[SourceItem]:
        # Sort by trading volume so we surface the notable markets, not the
        # high-frequency intraday crypto candles.
        params = {"active": "true", "closed": "false", "limit": self.max_markets,
                  "order": "volume24hr", "ascending": "false"}
        try:
            resp = requests.get(GAMMA, params=params, timeout=self.timeout,
                                headers={"User-Agent": "trump-stock-alerts/1.0"})
        except requests.RequestException as exc:
            logger.warning("[%s] Polymarket error: %s", self.name, exc)
            self.touch()
            return []
        if resp.status_code != 200:
            logger.warning("[%s] Polymarket HTTP %s", self.name, resp.status_code)
            self.touch()
            return []
        try:
            markets = resp.json()
        except ValueError:
            self.touch()
            return []
        if isinstance(markets, dict):
            markets = markets.get("data", [])

        items: List[SourceItem] = []
        for m in markets:
            question = clean(m.get("question", ""))
            mid = str(m.get("id") or m.get("conditionId") or "")
            if not question or not mid or not is_market_relevant(question):
                continue
            # Only high-conviction (>= min_probability) and high-volume markets.
            prob = lead_probability(m.get("outcomes"), m.get("outcomePrices"))
            if prob < self.min_probability:
                continue
            try:
                volume = float(m.get("volume") or 0)
            except (TypeError, ValueError):
                volume = 0.0
            if volume < self.min_volume:
                continue
            if db.source_item_exists(self.conn, self.name, mid):
                continue
            pct = yes_probability(m.get("outcomePrices"), m.get("outcomes"))
            slug = m.get("slug", "")
            vol_str = f"${volume/1e6:.1f}M" if volume >= 1e6 else f"${volume/1e3:.0f}K"
            text = f"{question} — {pct} ({vol_str} vol)"
            items.append(SourceItem(
                source=self.name,
                source_item_id=mid,
                url=f"https://polymarket.com/market/{slug}" if slug else "https://polymarket.com",
                text=text,
                timestamp=m.get("startDate") or m.get("createdAt") or now_iso(),
                title=question,
            ))
            if len(items) >= self.max_emit:
                break
        self.touch()
        return items
