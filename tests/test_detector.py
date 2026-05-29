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
    with (BASE_DIR / "config" / "priority_tickers.json").open() as fh:
        priority = json.load(fh)
    resolver = TickerResolver(
        watchlist=watchlist,
        universe_path=BASE_DIR / "data" / "stock_universe.csv",
        enable_online=False,
        index_tickers=set(priority.get("ALL", [])),
    )
    return Detector(resolver=resolver, phrases=phrases, watchlist=watchlist,
                    priority_tickers=priority, use_spacy=False)


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


# --- real-world Trump examples (must not be missed) ------------------------- #
def test_real_dell_go_buy(detector):
    by = _by_ticker(detector.detect("Go buy a Dell, they are amazing."))
    assert by["DELL"].confidence == Confidence.HIGH


def test_real_pltr_are_amazing(detector):
    # bare ticker + praise ("are amazing") -> MEDIUM, even with no "stock" word.
    by = _by_ticker(detector.detect("PLTR are amazing, they helped us in the war."))
    assert "PLTR" in by
    assert by["PLTR"].confidence == Confidence.MEDIUM


def test_real_palantir_is_amazing(detector):
    by = _by_ticker(detector.detect("Palantir is amazing, they helped us."))
    assert by["PLTR"].confidence == Confidence.MEDIUM


def test_real_intc_bought_and_up(detector):
    # "he bought" (ownership) + "up 250%" (performance) -> HIGH.
    by = _by_ticker(detector.detect("INTC is up 250% after he bought it."))
    assert "INTC" in by
    assert by["INTC"].confidence == Confidence.HIGH


def test_real_amd_doing_amazing(detector):
    by = _by_ticker(detector.detect("I think AMD is doing amazing things."))
    assert by["AMD"].confidence == Confidence.MEDIUM


def test_real_amp_is_dynamic_not_hardcoded(detector):
    # AMP (Ameriprise) is NOT in the watchlist — proves full-universe detection.
    by = _by_ticker(detector.detect("You should all buy AMP, great company."))
    assert "AMP" in by
    assert by["AMP"].confidence == Confidence.HIGH


def test_contract_mention_stays_low(detector):
    # A factual contract mention is not a stock-call -> must not alert (LOW).
    by = _by_ticker(detector.detect("Boeing got a huge contract today."))
    assert by["BA"].confidence == Confidence.LOW


def test_performance_up_percent_is_medium(detector):
    by = _by_ticker(detector.detect("NVDA is up 40% this week."))
    assert by["NVDA"].confidence == Confidence.MEDIUM


# --- index-membership precision -------------------------------------------- #
def test_offindex_acronym_with_only_praise_is_ignored(detector):
    # NRC / SMR are off-index acronyms; praise alone (no stock word) must NOT
    # surface them as tickers.
    results = detector.detect("The NRC and SMR reactor program is amazing.")
    tickers = {r.ticker for r in results}
    assert "NRC" not in tickers
    assert "SMR" not in tickers


def test_offindex_with_real_stock_context_still_detected(detector):
    # Off-index tickers are NOT ignored — with a real stock word they still fire.
    by = _by_ticker(detector.detect("USAR stock is amazing, people are buying it."))
    assert "USAR" in by  # USA Rare Earth, not in the index, still caught


def test_index_membership_labelled(detector):
    by = _by_ticker(detector.detect("Go buy a Dell, amazing."))
    assert "S&P 500" in by["DELL"].in_index


def test_index_ticker_praise_only_alerts(detector):
    # PLTR is in the index -> praise alone is enough (MEDIUM).
    by = _by_ticker(detector.detect("PLTR are amazing."))
    assert by["PLTR"].confidence == Confidence.MEDIUM


# --- bullish vs bearish direction ------------------------------------------ #
def test_bullish_direction(detector):
    by = _by_ticker(detector.detect("Go out and buy a Dell, they're great."))
    assert by["DELL"].direction == "bullish"


def test_bearish_attack_detected(detector):
    # Trump attacking a company is also market-moving -> bearish.
    by = _by_ticker(detector.detect("Boeing is a total disaster, a complete disaster."))
    assert "BA" in by
    assert by["BA"].direction == "bearish"
    assert by["BA"].confidence == Confidence.HIGH


def test_bearish_tariff_medium(detector):
    by = _by_ticker(detector.detect("We are putting a big tariff on Boeing."))
    assert "BA" in by
    assert by["BA"].direction == "bearish"


def test_bearish_negative_performance(detector):
    by = _by_ticker(detector.detect("NVDA plunged 20% today."))
    assert by["NVDA"].direction == "bearish"
    assert by["NVDA"].confidence == Confidence.MEDIUM


def test_contract_catalyst_phrase(detector):
    by = _by_ticker(detector.detect("Dell awarded a Pentagon government contract worth $2B."))
    assert "DELL" in by
    assert by["DELL"].confidence == Confidence.MEDIUM
    assert by["DELL"].direction == "bullish"
