"""Tests for the API-based corroboration sources (offline / mocked)."""

from types import SimpleNamespace

import pytest

import db
import sources.gdelt_source as gd
from sources import build_sources
from sources.gdelt_source import GDELTSource
from sources.youtube_source import _fetch_transcript


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_gdelt_parses_and_normalizes_time(conn, monkeypatch):
    payload = {"articles": [
        {"url": "https://x.com/a", "title": "Trump says buy Dell", "domain": "x.com",
         "seendate": "20260528T105916Z"},
        {"url": "https://y.com/b", "title": "Markets react", "domain": "y.com",
         "seendate": "20260528T110000Z"},
    ]}
    monkeypatch.setattr(gd.requests, "get", lambda *a, **k: _Resp(payload))
    src = GDELTSource(conn=conn, name="t", query="Trump buy")
    items = src.fetch_new_items()
    assert len(items) == 2
    assert items[0].url == "https://x.com/a"
    assert items[0].timestamp == "2026-05-28T10:59:16+00:00"  # normalized to ISO
    assert "Dell" in items[0].text
    # main.process_item is what persists items; simulate it, then the adapter's
    # source_item_exists check should skip them on the next fetch.
    for it in items:
        db.insert_source_item(conn, it)
    assert src.fetch_new_items() == []


def test_gdelt_handles_bad_response(conn, monkeypatch):
    class _Bad:
        status_code = 500
        text = "err"
    monkeypatch.setattr(gd.requests, "get", lambda *a, **k: _Bad())
    assert GDELTSource(conn=conn, name="t", query="q").fetch_new_items() == []


def test_youtube_transcript_fallback_is_safe():
    # Garbage id must not raise; returns "" when captions unavailable/lib missing.
    assert _fetch_transcript("___not_a_real_video___") == ""


def _settings(youtube=None, newsapi=None):
    return SimpleNamespace(
        x_bearer_token=None, youtube_api_key=youtube, newsapi_key=newsapi,
    )


def test_build_sources_gdelt_keyless_and_keyed_guards(conn):
    cfg = {
        "gdelt": {"enabled": True, "queries": [{"name": "q", "query": "Trump buy"}]},
        "youtube": {"enabled": True, "channels": [{"name": "WH", "handle": "@WhiteHouse"}]},
        "newsapi": {"enabled": True, "queries": ["Trump buy"]},
    }
    # No keys: GDELT builds (keyless); youtube/newsapi are skipped.
    built = build_sources(cfg, conn, _settings())
    names = [s.name for s in built]
    assert any(n.startswith("gdelt:") for n in names)
    assert not any(n.startswith("youtube:") for n in names)
    assert not any(n.startswith("newsapi:") for n in names)

    # With keys: all three build.
    built2 = build_sources(cfg, conn, _settings(youtube="yk", newsapi="nk"))
    names2 = [s.name for s in built2]
    assert any(n.startswith("youtube:") for n in names2)
    assert any(n.startswith("newsapi:") for n in names2)


def test_gdelt_priority_is_secondary(conn):
    cfg = {"gdelt": {"enabled": True, "queries": [{"name": "q", "query": "Trump"}]}}
    built = build_sources(cfg, conn, _settings())
    gdelt = [s for s in built if s.name.startswith("gdelt:")][0]
    assert gdelt.priority == "SECONDARY"
    assert gdelt.require_keywords  # non-primary -> trump keyword filter applied


def test_usaspending_parses_awards(conn, monkeypatch):
    import sources.usaspending_source as us

    class _R:
        status_code = 200

        def json(self):
            return {"results": [
                {"Award ID": "AW1", "Recipient Name": "DELL TECHNOLOGIES INC",
                 "Award Amount": 2.1e9, "Awarding Agency": "Department of Defense",
                 "Start Date": "2026-05-28", "Description": "IT services"},
            ]}
    monkeypatch.setattr(us.requests, "post", lambda *a, **k: _R())
    src = us.USASpendingSource(conn=conn, min_amount=1e8)
    items = src.fetch_new_items()
    assert len(items) == 1
    assert "awarded a Department of Defense government contract" in items[0].text
    assert "$2.1B" in items[0].text
    assert items[0].source_item_id == "AW1"


def test_usaspending_builds_as_primary(conn):
    built = build_sources({"usaspending": {"enabled": True}}, conn, _settings())
    us = [s for s in built if s.name.startswith("usaspending:")]
    assert us and us[0].priority == "PRIMARY"
    assert us[0].require_keywords == []  # company is the subject; no Trump filter
