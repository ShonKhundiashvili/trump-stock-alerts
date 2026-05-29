"""Tests for the detection pipeline.

These tests run WITHOUT the spaCy model (use_spacy=False) so they are fast and
deterministic; watchlist-alias + cashtag + ticker-token detection cover all the
required cases. spaCy NER adds dynamic detection beyond the watchlist at runtime.
"""

import json
from pathlib import Path

import pytest

from detector import Detector
from models import Confidence
from ticker_resolver import TickerResolver

BASE_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def detector():
    with (BASE_DIR / "config" / "watchlist.json").open() as fh:
        watchlist = json.load(fh)
    with (BASE_DIR / "config" / "phrases.json").open() as fh:
        phrases = json.load(fh)
    resolver = TickerResolver(
        watchlist=watchlist,
        universe_path=BASE_DIR / "data" / "stock_universe.csv",
        enable_online=False,
    )
    return Detector(resolver=resolver, phrases=phrases, watchlist=watchlist, use_spacy=False)


def _by_ticker(results):
    return {r.ticker: r for r in results}


def test_go_out_and_buy_a_dell_is_high(detector):
    results = detector.detect("Go out and buy a Dell, they're great.")
    by = _by_ticker(results)
    assert "DELL" in by
    assert by["DELL"].confidence == Confidence.HIGH
    assert by["DELL"].company_name == "Dell Technologies Inc."
    assert by["DELL"].matched_phrase == "go out and buy"


def test_i_bought_intel_is_high(detector):
    results = detector.detect("I bought Intel last week.")
    by = _by_ticker(results)
    assert "INTC" in by
    assert by["INTC"].confidence == Confidence.HIGH


def test_cashtag_nvda(detector):
    results = detector.detect("Watching $NVDA closely.")
    by = _by_ticker(results)
    assert "NVDA" in by
    # No buy/positive phrase -> LOW, but still detected.
    assert by["NVDA"].confidence == Confidence.LOW


def test_tesla_great_company_is_medium(detector):
    results = detector.detect("Tesla is a great company.")
    by = _by_ticker(results)
    assert "TSLA" in by
    assert by["TSLA"].confidence == Confidence.MEDIUM


def test_company_only_mention_is_low(detector):
    results = detector.detect("I met with the CEO of Boeing today.")
    by = _by_ticker(results)
    assert "BA" in by
    assert by["BA"].confidence == Confidence.LOW


def test_unrelated_text_no_match(detector):
    assert detector.detect("The weather is lovely today and I went for a walk.") == []


def test_false_positive_usa(detector):
    # USA must not be treated as a ticker.
    results = detector.detect("The USA is a great nation.")
    tickers = {r.ticker for r in results}
    assert "USA" not in tickers
    assert results == []


def test_false_positive_ceo(detector):
    results = detector.detect("The CEO gave a speech about GDP and the economy.")
    tickers = {r.ticker for r in results}
    assert "CEO" not in tickers
    assert "GDP" not in tickers


def test_multiple_companies(detector):
    results = detector.detect("I bought Apple and Microsoft.")
    by = _by_ticker(results)
    assert "AAPL" in by
    assert "MSFT" in by
    assert by["AAPL"].confidence == Confidence.HIGH
    assert by["MSFT"].confidence == Confidence.HIGH


def test_buy_ticker_token_in_context(detector):
    # Bare uppercase token trusted only in stock context.
    results = detector.detect("People should buy DELL shares now.")
    by = _by_ticker(results)
    assert "DELL" in by
    assert by["DELL"].confidence == Confidence.HIGH
