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


def _settings(youtube=None, newsapi=None, kalshi=None, fmp=None):
    return SimpleNamespace(
        x_bearer_token=None, youtube_api_key=youtube, newsapi_key=newsapi,
        kalshi_api_key=kalshi, fmp_api_key=fmp,
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
         "slug": "tesla-spacex", "outcomes": "[\"Yes\", \"No\"]",
         "outcomePrices": "[\"0.80\", \"0.20\"]", "volume": 2000000,
         "startDate": "2026-05-29T10:00:00Z"},
        {"id": "2", "question": "Will Karen Bass win LA mayor?",
         "slug": "labass", "outcomes": "[\"Yes\",\"No\"]",
         "outcomePrices": "[\"0.4\",\"0.6\"]", "volume": 2000000,
         "startDate": "2026-05-29T10:00:00Z"},
        {"id": "3", "question": "Will Bitcoin hit $200k this year?",
         "slug": "btc", "outcomes": "[\"Yes\",\"No\"]",
         "outcomePrices": "[\"0.20\",\"0.80\"]", "volume": 2000000,
         "startDate": "2026-05-29T10:00:00Z"},
    ]

    class _R:
        status_code = 200

        def json(self):
            return payload
    monkeypatch.setattr(pm.requests, "get", lambda *a, **k: _R())
    items = pm.PolymarketSource(conn=conn).fetch_new_items()
    # politics market filtered out; Bitcoin filtered out (20% < 50% probability)
    assert len(items) == 1
    assert "Tesla and SpaceX merge" in items[0].text
    assert "80%" in items[0].text


def test_kalshi_parses_and_filters(conn, monkeypatch):
    import sources.kalshi_source as ks
    payload = {"markets": [
        {"ticker": "KXNVDA", "title": "Will Nvidia acquire a chip startup in 2026?",
         "category": "Companies", "last_price": 60, "open_time": "2026-05-29T10:00:00Z"},
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


# --- analyst ratings (FMP) + SEC stakes ------------------------------------ #
def test_ratings_parses_fmp(conn, monkeypatch):
    import sources.ratings_source as rs
    rows = [{"symbol": "NVDA", "gradingCompany": "Morgan Stanley",
             "previousGrade": "Equal-Weight", "newGrade": "Overweight",
             "action": "upgrade", "priceTarget": 200,
             "publishedDate": "2026-05-29T10:00:00Z", "newsURL": "http://x"}]

    class _R:
        status_code = 200

        def json(self):
            return rows
    monkeypatch.setattr(rs.requests, "get", lambda *a, **k: _R())
    items = rs.RatingsSource(conn=conn, api_key="k").fetch_new_items()
    assert len(items) == 1
    assert items[0].ticker == "NVDA"
    assert "Morgan Stanley" in items[0].text and "Overweight" in items[0].text


def test_ratings_skipped_without_key(conn):
    from sources.ratings_source import RatingsSource
    assert RatingsSource(conn=conn, api_key=None).fetch_new_items() == []


def test_sec_stakes_parses(conn, monkeypatch):
    import sources.sec_stakes_source as ss
    resp_json = {"hits": {"hits": [
        {"_id": "0001104659-26-066737:doc.htm",
         "_source": {"file_type": "SC 13G", "file_date": "2026-05-28",
                     "display_names": ["TESLA INC  (TSLA)  (CIK 0001318605)",
                                       "BlackRock Inc.  (BLK)  (CIK 0001364742)"]}},
    ]}}

    class _R:
        status_code = 200

        def json(self):
            return resp_json
    monkeypatch.setattr(ss.requests, "get", lambda *a, **k: _R())
    items = ss.SECStakesSource(conn=conn, filers=["BlackRock"]).fetch_new_items()
    assert items
    assert items[0].title == "TESLA INC"           # subject company (not the filer)
    assert "BlackRock filed SC 13G on TESLA INC" in items[0].text


def test_new_relay_sources_route(conn):
    cfg = {"ratings": {"enabled": True}, "sec_stakes": {"enabled": True}}
    built = build_sources(cfg, conn, _settings(fmp="k"))
    by = {s.name.split(":")[0]: s for s in built}
    assert by["ratings"].relay and by["ratings"].channel == "ratings"
    assert by["sec"].relay and by["sec"].channel == "institutions"


def test_institution_action_filter():
    from sources.institutions_news_source import institution_action_relevant as r
    assert r("BlackRock takes 8.1% stake in Archer Aviation")
    assert r("Vanguard discloses new 5% position in Acme Corp")
    assert r("BlackRock files 13D on XYZ, activist stake")
    assert r("Vanguard trims stake in Boeing")
    # Noise: passive mention / price buzz / ratings -> excluded
    assert not r("SLS stock hits 4-year high: BlackRock stake boost fuels buzz")
    assert not r("Why Nvidia stock soared after BlackRock comments")
    assert not r("Morgan Stanley upgrades Tesla to Overweight")


def test_market_news_notable_filter():
    from sources.market_news_source import market_news_notable as r
    assert r("SpaceX reportedly looking to go public at valuation of $1.8 trillion")
    assert r("Warren Buffett indicator hits all-time high — most expensive stock market")
    assert r("Nvidia hits $5 trillion market cap")
    assert r("Bitcoin hits all-time high above $150,000")
    assert not r("Local bakery wins small business award")
    assert not r("Trump praises economy at rally")


def test_market_news_routes_to_predictions(conn):
    built = build_sources({"market_news": {"enabled": True, "queries": ["SpaceX IPO"]}},
                          conn, _settings())
    mn = [s for s in built if s.name.startswith("marketnews:")]
    assert mn and mn[0].relay and mn[0].channel == "predictions"
