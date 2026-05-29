"""AUTO-GENERATED from training_examples — DO NOT EDIT BY HAND.

Regenerate with:
    python scripts/generate_tests_from_training.py

Each case comes from a 🧪 "Mark as Training Example" you saved in Telegram.
"""

import json
from pathlib import Path

import pytest

from detector import Detector
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


CASES = json.loads(r"""
[
  {
    "id": "no_examples",
    "expect": "skip",
    "ticker": "",
    "text": "",
    "reason": "No training examples saved yet."
  }
]
""")


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_training_example(detector, case):
    if case["expect"] == "skip":
        pytest.skip(case["reason"])
    tickers = {r.ticker for r in detector.detect(case["text"])}
    if case["expect"] == "detect":
        assert case["ticker"] in tickers, (
            f"expected {case['ticker']} to be detected in: {case['text']!r}"
        )
    else:  # reject
        assert case["ticker"] not in tickers, (
            f"expected {case['ticker']} NOT to be detected in: {case['text']!r}"
        )
