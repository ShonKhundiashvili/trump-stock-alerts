"""Institutional-holder news (Google News) — year-round big-holder coverage.

SEC 13D/13G filings are accurate but seasonal (13G annual filings cluster in
February). This source forwards keyword news about major holders' moves
(BlackRock, Vanguard, etc.) for stocks AND crypto (e.g. BlackRock's Bitcoin ETF)
to the institutions channel, so the room is useful all year. Keyless Google-News
RSS; relayed as-is.
"""

from __future__ import annotations

import sqlite3
from typing import List

from models import SourceItem
from .news_search_source import NewsSearchSource


class InstitutionsNewsSource(NewsSearchSource):
    type = "instnews"

    def __init__(self, conn: sqlite3.Connection, query: str, max_emit: int = 4) -> None:
        super().__init__(conn=conn, query=query)
        self.name = f"instnews:{query}"
        self.query = query
        self.max_emit = max_emit

    def fetch_new_items(self) -> List[SourceItem]:
        # Cap per cycle so a multi-query backlog can't flood the room.
        return super().fetch_new_items()[: self.max_emit]
