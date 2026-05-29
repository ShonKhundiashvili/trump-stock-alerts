"""Lightweight market-data helper (yfinance) for alert enrichment.

Provides a per-ticker quote (price, ATR, recent move) used to:
  - flag whether a move already ran ("too late?") vs still fresh, and
  - build a research trade plan (entry / ATR stop / target / position size).

RESEARCH ONLY — not financial advice, no guarantees. A small per-run cache
avoids refetching the same ticker within a cycle.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class Quote:
    ticker: str
    price: float
    atr: float
    atr_pct: float
    move_1d: float    # last close vs prior close
    move_5d: float    # last close vs 5 sessions ago


def quote(ticker: str, cache: Optional[Dict[str, "Quote"]] = None) -> Optional[Quote]:
    if not ticker:
        return None
    sym = ticker.replace(".", "-").upper()
    if cache is not None and sym in cache:
        return cache[sym]
    try:
        import yfinance as yf
        df = yf.Ticker(sym).history(period="2mo")
        if df is None or len(df) < 15:
            return _cache(cache, sym, None)
        close, high, low = df["Close"], df["High"], df["Low"]
        price = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        five = float(close.iloc[-6]) if len(close) >= 6 else prev
        # ATR(14)
        import pandas as pd
        pc = close.shift()
        tr = pd.concat([(high - low), (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])
        q = Quote(sym, round(price, 2), round(atr, 2),
                  round(atr / price * 100, 2) if price else 0.0,
                  round(price / prev - 1, 4) if prev else 0.0,
                  round(price / five - 1, 4) if five else 0.0)
        return _cache(cache, sym, q)
    except Exception as exc:  # noqa: BLE001 - never break an alert on a data hiccup
        logger.debug("quote(%s) failed: %s", sym, exc)
        return _cache(cache, sym, None)


def _cache(cache, sym, val):
    if cache is not None:
        cache[sym] = val
    return val


def too_late_flag(q: Quote, direction: str = "bullish") -> str:
    """Has the move likely already run? Heuristic from recent price action."""
    if not q:
        return ""
    m5 = q.move_5d * 100
    if direction == "bullish" and (q.move_5d >= 0.06 or q.move_1d >= 0.05):
        return f"⚠️ already +{m5:.1f}% over ~5d — may be late"
    if direction == "bearish" and (q.move_5d <= -0.06 or q.move_1d <= -0.05):
        return f"⚠️ already {m5:.1f}% over ~5d — may be late"
    return f"✅ fresh ({m5:+.1f}% over ~5d)"


def already_ran(q: Optional["Quote"], direction: str, threshold_pct: float) -> bool:
    """True if the move already happened — used to suppress 'too-late' buy/sell calls.

    Bullish/neutral: a recent run-up of >= threshold (1d or 5d). Bearish: an
    equivalent drop. Fails open (False) on missing data or a disabled threshold.
    """
    if not q or not threshold_pct:
        return False
    m1, m5 = q.move_1d * 100, q.move_5d * 100
    if direction == "bearish":
        return m1 <= -threshold_pct or m5 <= -threshold_pct
    return m1 >= threshold_pct or m5 >= threshold_pct


def trade_plan(q: Quote, direction: str, account_size: float, risk_pct: float,
               target_pct: float = 0.05) -> str:
    """Research trade plan: entry / ATR stop / target / position size. Not advice."""
    if not q or q.atr <= 0 or q.price <= 0:
        return ""
    bullish = direction != "bearish"
    entry = q.price
    if bullish:
        stop = round(entry - 1.0 * q.atr, 2)
        target = round(entry * (1 + target_pct), 2)
    else:
        stop = round(entry + 1.0 * q.atr, 2)
        target = round(entry * (1 - target_pct), 2)
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return ""
    risk_dollars = account_size * (risk_pct / 100.0)
    shares = int(risk_dollars // risk_per_share)
    stop_pct = (stop / entry - 1) * 100
    tgt_pct = (target / entry - 1) * 100
    rr = abs(target - entry) / risk_per_share
    return (f"📐 Plan (research): entry ~${entry:g} · stop ${stop:g} ({stop_pct:+.1f}%) · "
            f"target ${target:g} ({tgt_pct:+.1f}%) · RR {rr:.1f} · "
            f"~{shares} sh for {risk_pct:g}% risk (${risk_dollars:,.0f})")
