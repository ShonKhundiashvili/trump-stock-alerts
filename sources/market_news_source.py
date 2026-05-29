"""Hot market-news source — big stock/crypto headlines (IPOs, mergers, records).

The Kalshi/Polymarket X accounts mostly amplify financial NEWS (e.g. "SpaceX to
go public at $1.8T", "Buffett indicator at all-time high"). Those are news
reports, not >50% prediction markets, and aren't Trump-specific — so the normal
feeds miss them. This source forwards NOTABLE market events (Google News,
keyless) to the predictions (stock/crypto) room, filtered to real catalysts.
"""

from __future__ import annotations

import re
import sqlite3
from typing import List

from models import SourceItem
from .news_search_source import NewsSearchSource

_SUBJECT = (
    "stock", "shares", "market", "wall street", "s&p", "nasdaq", "dow",
    "bitcoin", "btc", "ethereum", "crypto", "solana", "xrp", "dogecoin",
    "spacex", "tesla", "nvidia", "apple", "microsoft", "amazon", "meta",
    "google", "openai", "palantir", "coreweave", "anthropic", "starlink",
)
_EVENT_RE = re.compile(
    r"\b(ipo|go(es)? public|public offering|direct listing|merger|merges?|"
    r"acquir\w+|takeover|buyout|all-?time high|record high|record low|"
    r"\$\s?\d[\d.,]*\s*(billion|trillion|b\b|t\b)|market cap|valuation|"
    r"halving|spin[- ]?off|bankruptcy|delist)\b",
    re.I,
)


def market_news_notable(title: str) -> bool:
    """True only for a notable stock/crypto market event (not routine news)."""
    if not title:
        return False
    t = f" {title.lower()} "
    return any(s in t for s in _SUBJECT) and bool(_EVENT_RE.search(title))


class MarketNewsSource(NewsSearchSource):
    type = "marketnews"

    def __init__(self, conn: sqlite3.Connection, query: str, max_emit: int = 4) -> None:
        super().__init__(conn=conn, query=query)
        self.name = f"marketnews:{query}"
        self.query = query
        self.max_emit = max_emit

    def fetch_new_items(self) -> List[SourceItem]:
        items = [it for it in super().fetch_new_items()
                 if market_news_notable(it.title or it.text)]
        return items[: self.max_emit]
