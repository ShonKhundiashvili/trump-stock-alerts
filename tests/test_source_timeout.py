"""safe_fetch must bound both failures and hangs so one source can't freeze the bot."""

from __future__ import annotations

import time

import db
from models import SourceItem
from sources.base import BaseSource


class _HangingSource(BaseSource):
    type = "test"

    def fetch_new_items(self):
        time.sleep(30)  # simulate a stuck socket read
        return []


class _SlowButOkSource(BaseSource):
    type = "test"

    def fetch_new_items(self):
        return [SourceItem(source=self.name, source_item_id="1", url="http://x",
                           text="hi", timestamp="2026-05-29T10:00:00Z")]


class _BoomSource(BaseSource):
    type = "test"

    def fetch_new_items(self):
        raise RuntimeError("boom")


def _conn():
    c = db.connect(":memory:")
    db.init_db(c)
    return c


def test_hanging_source_times_out_fast():
    src = _HangingSource(_conn(), name="test:hang")
    t0 = time.monotonic()
    items = src.safe_fetch(timeout=0.5)
    elapsed = time.monotonic() - t0
    assert items == []
    assert elapsed < 5  # returned promptly, did not wait the full 30s


def test_ok_source_returns_items_and_stamps_routing():
    src = _SlowButOkSource(_conn(), name="test:ok")
    src.channel = "markets"
    src.priority = "SECONDARY"
    items = src.safe_fetch(timeout=5)
    assert len(items) == 1
    assert items[0].channel == "markets"
    assert items[0].priority == "SECONDARY"


def test_raising_source_is_isolated():
    src = _BoomSource(_conn(), name="test:boom")
    assert src.safe_fetch(timeout=5) == []
