"""Institutional-holder news (Google News) — year-round big-holder coverage.

SEC 13D/13G filings are accurate but seasonal (13G annual filings cluster in
February). This source forwards keyword news about major holders' moves
(BlackRock, Vanguard, etc.) for stocks AND crypto (e.g. BlackRock's Bitcoin ETF)
to the institutions channel, so the room is useful all year. Keyless Google-News
RSS; relayed as-is.
"""

from __future__ import annotations

import re
import sqlite3
from typing import List

from models import SourceItem
from .news_search_source import NewsSearchSource

# A real holder ACTION must be named (not a passive "X holds shares" mention).
_HOLDERS = ("blackrock", "vanguard", "state street", "berkshire", "fidelity",
            "capital group", "t. rowe", "t rowe", "geode", "ark invest",
            "citadel", "bridgewater", "renaissance technologies")
_ACTION_RE = re.compile(
    r"\b(take|takes|took|raise|raises|raised|increase|increases|increased|"
    r"boost|boosts|cut|cuts|trim|trims|sell|sells|sold|exit|exits|acquire|"
    r"acquires|acquired|buy|buys|bought|disclose|discloses|report|reports|"
    r"new|adds|adding)\s+(a\s+|its\s+|the\s+)?(\d[\d.]*%?\s+)?"
    r"(stake|position|shares|holding|stakes)\b",
    re.I,
)
_THIRTEEN_RE = re.compile(r"\b(13d|13g|activist (stake|position|investor))\b", re.I)
# Buzz / price-action / ratings noise that isn't a holder action.
_EXCLUDE_RE = re.compile(
    r"\b(fuels? buzz|year high|all-time high|record high|price target|upgrade|"
    r"downgrade|analyst|rating|surges?|soars?|jumps?|rallies|why \w+ stock)\b",
    re.I,
)


def institution_action_relevant(title: str) -> bool:
    """True only for an actual big-holder action (stake change / 13D-G / activist)."""
    if not title:
        return False
    t = title.lower()
    if not any(h in t for h in _HOLDERS):
        return False
    if _EXCLUDE_RE.search(t):
        return False
    return bool(_ACTION_RE.search(title) or _THIRTEEN_RE.search(title))


class InstitutionsNewsSource(NewsSearchSource):
    type = "instnews"

    def __init__(self, conn: sqlite3.Connection, query: str, max_emit: int = 4) -> None:
        super().__init__(conn=conn, query=query)
        self.name = f"instnews:{query}"
        self.query = query
        self.max_emit = max_emit

    def fetch_new_items(self) -> List[SourceItem]:
        items = [it for it in super().fetch_new_items()
                 if institution_action_relevant(it.title or it.text)]
        return items[: self.max_emit]
