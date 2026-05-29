"""Relevance filter for prediction-market questions (Kalshi / Polymarket).

We only want stock / crypto / company / M&A markets — NOT politics, sports,
celebrities, or weather. A market is relevant if its question mentions any of
the INCLUDE terms below. (Pulling from the market APIs already excludes the
recap-style tweets, since those aren't markets.)
"""

from __future__ import annotations

import re

# The SUBJECT must be a stock / crypto / index / known company.
SUBJECT_TERMS = [
    # crypto
    "bitcoin", "btc", "ethereum", " eth ", "ether", "crypto", "solana", " sol ",
    "dogecoin", "doge", "xrp", "ripple", "cardano", "stablecoin", "coinbase",
    "altcoin", "memecoin", "binance",
    # stocks / markets / indices
    "stock", "shares", "share price", "s&p", "s&p 500", "nasdaq", "dow jones",
    "ticker", "valuation",
    # well-known companies
    "tesla", "spacex", "nvidia", "nvda", "apple", "microsoft", "amazon", "meta",
    "google", "alphabet", "openai", "palantir", "amd", "intel", "dell", "boeing",
    "coreweave", "crwv", "starlink", "anthropic", "broadcom", "oracle", "netflix",
]

# A substantive EVENT must be implied (not an intraday up/down candle).
EVENT_TERMS = [
    "merge", "merger", "acquire", "acquisition", "acquired", "partnership",
    "partner with", "buyout", "takeover", "invests", "invest in", "investment in",
    "stake in", "spin off", "spinoff", "joint venture", "ipo", "public offering",
    "market cap", "all-time high", "all time high", "record high", "delist",
    "bankruptcy", "bankrupt", "hit ", "hits ", "reach", "reaches", "above ",
    "below ", "close above", "close below", "by 2026", "by 2027", "by end of",
    "this year", "earnings", "split", "buyback", "dividend",
]

# Spammy / intraday micro-markets to exclude even if they mention a subject.
_EXCLUDE_RE = [
    re.compile(r"\bup or down\b"),
    # clock-time ranges like "7:20AM-7:25AM ET" or "1PM-2PM"
    re.compile(r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b.*\b\d{1,2}(:\d{2})?\s?(am|pm)\b"),
    re.compile(r"\bnext (5|10|15|30) minutes\b"),
    re.compile(r"\bin the next hour\b"),
]

_PRICE_RE = re.compile(r"\$\s?\d")


def is_market_relevant(question: str) -> bool:
    """True only for substantive stock/crypto/company/M&A markets.

    Requires a SUBJECT (stock/crypto/company/index) AND an EVENT (merge, IPO,
    price target, etc.), and excludes intraday "up or down" micro-markets.
    """
    if not question:
        return False
    q = f" {question.lower()} "
    if any(rx.search(q) for rx in _EXCLUDE_RE):
        return False
    has_subject = any(t in q for t in SUBJECT_TERMS)
    has_event = any(t in q for t in EVENT_TERMS) or bool(_PRICE_RE.search(question))
    return has_subject and has_event


def _as_list(x):
    if isinstance(x, str):
        import json
        try:
            return json.loads(x)
        except ValueError:
            return []
    return x or []


def lead_probability(outcomes, prices) -> float:
    """Probability (0-1) of the headline outcome.

    For a Yes/No market it's the 'Yes' price (the chance the event happens);
    for a multi-outcome market it's the leading outcome's price. Returns 0 on
    parse failure.
    """
    outs = [str(o).lower() for o in _as_list(outcomes)]
    prc = _as_list(prices)
    try:
        if "yes" in outs:
            return float(prc[outs.index("yes")])
        return max(float(p) for p in prc) if prc else 0.0
    except (ValueError, TypeError, IndexError):
        return 0.0


def yes_probability(prices, outcomes=None) -> str:
    """Format the headline probability as 'NN%'."""
    p = lead_probability(outcomes, prices)
    return f"{round(p * 100)}%" if p else "?"


_WS = re.compile(r"\s+")


def clean(text: str) -> str:
    return _WS.sub(" ", (text or "")).strip()
