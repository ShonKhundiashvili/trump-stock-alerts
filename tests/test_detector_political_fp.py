"""Regression: capitalized common words in political posts must NOT become tickers.

Trump capitalizes many ordinary words ("Great", "America", "Border", "Crime").
The fuzzy name->ticker resolver maps each of those to *some* ticker at
resolution_confidence 0.0 (e.g. "Great" -> CAR / Avis Budget Group). Before the
fix, NAME_MATCH_MIN_CONF was 0, so these zero-confidence matches passed the gate
and produced bogus alerts in the Trump room. The detector must reject any
name-derived ticker that doesn't clear a real confidence bar.
"""

import json
from pathlib import Path

import pytest

from detector import Detector
from ticker_resolver import TickerResolver

BASE_DIR = Path(__file__).resolve().parent.parent

# The actual post that produced the bogus CAR alert.
ENDORSEMENT = (
    "It is my Great Honor to endorse MAGA Warrior, Mike Mazzei, who is running "
    "for Governor of Oklahoma, a State which I love, and WON BIG - All 77 out of "
    "77 Counties in 2016, 2020, and will do even better in 2026. Mike Mazzei is a "
    "Great Patriot who will help us Make America Great Again. He is doing a great "
    "job and works very hard. DRILL, BABY, DRILL!!! President DJT"
)


@pytest.fixture
def detector():
    watchlist = json.loads((BASE_DIR / "config" / "watchlist.json").read_text())
    phrases = json.loads((BASE_DIR / "config" / "phrases.json").read_text())
    priority = json.loads((BASE_DIR / "config" / "priority_tickers.json").read_text())
    resolver = TickerResolver(watchlist=watchlist, enable_online=False,
                              index_tickers=set(priority.get("ALL", [])))
    return Detector(resolver=resolver, phrases=phrases, watchlist=watchlist,
                    priority_tickers=priority, use_spacy=False)


def test_political_endorsement_yields_no_ticker(detector):
    dets = detector.detect(ENDORSEMENT)
    tickers = [d.ticker for d in dets if d.ticker]
    assert tickers == [], f"political post should not resolve any ticker, got {tickers}"


@pytest.mark.parametrize("word", ["Great", "America", "Border", "Crime",
                                  "Military", "Patriot", "Veterans"])
def test_capitalized_common_words_dont_resolve(detector, word):
    text = f"This is a Great and {word} day for our Country. He did a great job."
    tickers = [d.ticker for d in detector.detect(text) if d.ticker]
    assert tickers == [], f"{word!r} should not resolve to a ticker, got {tickers}"


def test_real_company_name_still_detected(detector):
    # A genuine name match (conf ~95) must still pass the gate.
    text = "Apple is doing a great job, an incredible company."
    tickers = [d.ticker for d in detector.detect(text) if d.ticker]
    assert "AAPL" in tickers, f"expected AAPL, got {tickers}"
