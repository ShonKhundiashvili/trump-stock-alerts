"""Base interface for all source adapters."""

from __future__ import annotations

import abc
import logging
import sqlite3
from typing import List, Optional

import db
from models import SourceItem

logger = logging.getLogger(__name__)


class BaseSource(abc.ABC):
    """A pluggable source adapter.

    Subclasses implement `fetch_new_items()`, returning a list of new,
    normalized SourceItem objects. The adapter is responsible for tracking its
    own cursor (e.g. last seen id) via the source_state table helpers below.
    """

    type: str = "base"

    def __init__(self, conn: sqlite3.Connection, name: str) -> None:
        self.conn = conn
        self.name = name  # unique source key, e.g. "x:realDonaldTrump"
        self.priority: str = "PRIMARY"          # set by build_sources from config
        self.channel: str = "default"           # routing bucket (set by build_sources)
        self.require_keywords: list[str] = []   # if set, only items containing one
                                                # of these keywords are classified

    # -- cursor helpers ------------------------------------------------- #
    def get_last_seen_id(self) -> Optional[str]:
        return db.get_last_seen_id(self.conn, self.name)

    def set_last_seen_id(self, last_seen_id: Optional[str]) -> None:
        db.set_source_state(self.conn, self.name, last_seen_id=last_seen_id)

    def touch(self) -> None:
        """Record that we polled, without changing the cursor."""
        db.set_source_state(self.conn, self.name)

    # -- main API ------------------------------------------------------- #
    @abc.abstractmethod
    def fetch_new_items(self) -> List[SourceItem]:
        """Fetch and return new items since the last poll. Never raises."""
        raise NotImplementedError

    def safe_fetch(self) -> List[SourceItem]:
        """Wrapper that isolates failures so one bad source can't crash the bot."""
        try:
            items = self.fetch_new_items()
            for item in items:           # stamp provenance + routing onto every item
                item.priority = self.priority
                item.channel = self.channel
            logger.debug("[%s] fetched %d new item(s)", self.name, len(items))
            return items
        except Exception as exc:  # noqa: BLE001 - intentional broad catch
            logger.exception("[%s] fetch failed: %s", self.name, exc)
            return []
