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


def _settings(youtube=None, newsapi=None, kalshi=None):
    return SimpleNamespace(
        x_bearer_token=None, youtube_api_key=youtube, newsapi_key=newsapi,
        kalshi_api_key=kalshi,
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


# --- prediction markets (Polymarket / Kalshi) ------------------------------ #
def test_prediction_filter_relevance():
    from sources.prediction_filter import is_market_relevant
    assert is_market_relevant("Will Tesla and SpaceX merge within the next year?")
    assert is_market_relevant("Will Bitcoin hit $200k this year?")
    assert is_market_relevant("Will S&P 500 hit 8,000 this year?")
    assert is_market_relevant("Will NVDA invest in CoreWeave?")
    # Politics / people / sports -> excluded.
    assert not is_market_relevant("Will Karen Bass be re-elected as LA mayor?")
    assert not is_market_relevant("Peter Thiel relocates to Argentina?")
    assert not is_market_relevant("Who wins the NBA finals?")


def test_polymarket_parses_and_filters(conn, monkeypatch):
    import sources.polymarket_source as pm
    payload = [
        {"id": "1", "question": "Will Tesla and SpaceX merge in 2026?",
         "slug": "tesla-spacex", "outcomePrices": "[\"0.80\", \"0.20\"]",
         "startDate": "2026-05-29T10:00:00Z"},
        {"id": "2", "question": "Will Karen Bass win LA mayor?",
         "slug": "labass", "outcomePrices": "[\"0.4\",\"0.6\"]",
         "startDate": "2026-05-29T10:00:00Z"},
    ]

    class _R:
        status_code = 200

        def json(self):
            return payload
    monkeypatch.setattr(pm.requests, "get", lambda *a, **k: _R())
    items = pm.PolymarketSource(conn=conn).fetch_new_items()
    assert len(items) == 1  # politics market filtered out
    assert "Tesla and SpaceX merge" in items[0].text
    assert "80% yes" in items[0].text


def test_kalshi_parses_and_filters(conn, monkeypatch):
    import sources.kalshi_source as ks
    payload = {"markets": [
        {"ticker": "KXNVDA", "title": "Will Nvidia acquire a chip startup in 2026?",
         "category": "Companies", "last_price": 35, "open_time": "2026-05-29T10:00:00Z"},
        {"ticker": "KXNBA", "title": "Who wins the NBA finals?",
         "category": "Sports", "last_price": 50, "open_time": "2026-05-29T10:00:00Z"},
    ]}

    class _R:
        status_code = 200

        def json(self):
            return payload
    monkeypatch.setattr(ks.requests, "get", lambda *a, **k: _R())
    items = ks.KalshiSource(conn=conn).fetch_new_items()
    assert len(items) == 1
    assert "Nvidia acquire" in items[0].text


def test_prediction_sources_relay_and_route(conn):
    cfg = {"polymarket": {"enabled": True}, "kalshi": {"enabled": True}}
    built = build_sources(cfg, conn, _settings())
    pm = [s for s in built if s.name.startswith("polymarket:")][0]
    ks = [s for s in built if s.name.startswith("kalshi:")][0]
    assert pm.relay and ks.relay
    assert pm.channel == "predictions" and ks.channel == "predictions"
    assert pm.priority == "PRIMARY"
