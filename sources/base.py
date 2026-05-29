"""Base interface for all source adapters."""

from __future__ import annotations

import abc
import logging
import sqlite3
import threading
from typing import List, Optional

import db
from models import SourceItem

logger = logging.getLogger(__name__)

# Hard ceiling for a single source's fetch. Sources are polled sequentially in
# the cycle, so without this one source stuck on a socket read (a half-open
# connection, a hung server) would freeze the entire bot indefinitely. This
# bounds every source regardless of whether its own HTTP calls set a timeout.
DEFAULT_FETCH_TIMEOUT = 45.0


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
        self.relay: bool = False                # forward items as-is (no detector)
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

    def safe_fetch(self, timeout: float = DEFAULT_FETCH_TIMEOUT) -> List[SourceItem]:
        """Wrapper that isolates failures AND hangs so one bad source can't take
        down the bot.

        The fetch runs in a daemon thread bounded by `timeout`. If it overruns,
        we log and return [] so the cycle continues; the orphaned thread is a
        daemon and won't block process exit. Exceptions are caught the same way.
        """
        result: List[SourceItem] = []
        error: dict = {}

        def _run() -> None:
            try:
                result.extend(self.fetch_new_items())
            except Exception as exc:  # noqa: BLE001 - intentional broad catch
                error["exc"] = exc

        worker = threading.Thread(target=_run, name=f"fetch-{self.name}", daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            logger.error("[%s] fetch timed out after %.0fs; skipping this cycle",
                         self.name, timeout)
            return []
        if "exc" in error:
            logger.error("[%s] fetch failed: %s", self.name, error["exc"], exc_info=error["exc"])
            return []
        for item in result:              # stamp provenance + routing onto every item
            item.priority = self.priority
            item.channel = self.channel
            item.relay = self.relay
        logger.debug("[%s] fetched %d new item(s)", self.name, len(result))
        return result
