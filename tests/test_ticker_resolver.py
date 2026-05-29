"""Tests for ticker_resolver.TickerResolver."""

import json
from pathlib import Path

import pytest

from ticker_resolver import TickerResolver

BASE_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def watchlist():
    with (BASE_DIR / "config" / "watchlist.json").open() as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def resolver(watchlist):
    return TickerResolver(
        watchlist=watchlist,
        universe_path=BASE_DIR / "data" / "stock_universe.csv",
        enable_online=False,
    )


def test_dell(resolver):
    m = resolver.resolve("Dell")
    assert m.ticker == "DELL"
    assert m.resolution_confidence >= 90
    assert not m.ambiguous


def test_intel(resolver):
    assert resolver.resolve("Intel").ticker == "INTC"


def test_apple(resolver):
    assert resolver.resolve("Apple").ticker == "AAPL"


def test_nvidia(resolver):
    assert resolver.resolve("Nvidia").ticker == "NVDA"


def test_google_ambiguity_handling(resolver):
    # Google is a watchlist alias -> resolves to GOOGL, but GOOG is a valid variant.
    m = resolver.resolve("Google")
    assert m.ticker in ("GOOGL", "GOOG")


def test_unknown_company_no_confident_match(resolver):
    m = resolver.resolve("Zxqwerty Nonexistent Holdings")
    assert m.ticker is None
    assert m.resolution_confidence < 82


def test_ambiguous_fuzzy_lowers_confidence(resolver):
    # "American" matches American Express / Airlines / International Group similarly.
    m = resolver.resolve("American")
    assert m.ambiguous is True
    # confidence is capped when ambiguous
    assert m.resolution_confidence <= 80 or m.ticker is None


def test_direct_ticker_token(resolver):
    m = resolver.resolve_ticker_token("$NVDA")
    assert m is not None
    assert m.ticker == "NVDA"
    assert m.resolution_confidence >= 90


def test_short_word_not_matched(resolver):
    # 2-char non-ticker word should not resolve.
    m = resolver.resolve("of")
    assert m.ticker is None


def test_watchlist_priority_over_fuzzy(resolver):
    # Exact alias should win with watchlist confidence.
    m = resolver.resolve("Tesla")
    assert m.ticker == "TSLA"
    assert m.strategy == "watchlist"
