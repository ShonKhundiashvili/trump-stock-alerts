"""Tests for the weekly equity scanner (offline, synthetic price data)."""

import numpy as np
import pandas as pd
import pytest

import scanner


def _df(prices, vol=2_000_000):
    n = len(prices)
    close = pd.Series(prices, dtype=float)
    high = close * 1.01
    low = close * 0.99
    return pd.DataFrame({"Open": close, "High": high, "Low": low,
                         "Close": close, "Volume": [vol] * n})


def test_indicators_basic():
    s = pd.Series(range(1, 60), dtype=float)
    assert scanner.sma(s, 10).iloc[-1] == pytest.approx(s.tail(10).mean())
    r = scanner.rsi(s, 14).iloc[-1]
    assert r > 90  # straight uptrend -> high RSI
    line, sig, hist = scanner.macd(s)
    assert hist.iloc[-1] > 0  # rising series -> positive histogram


def test_atr_and_return():
    df = _df([100 + i * 0.1 for i in range(250)])
    a = scanner.atr(df["High"], df["Low"], df["Close"], 14).iloc[-1]
    assert a > 0
    assert scanner.pct_return(df["Close"], 63) > 0


def test_analyze_uptrend_produces_result():
    # Smooth steady uptrend -> should analyze, be above MAs, decent score.
    prices = [100 * (1.002 ** i) for i in range(260)]
    res = scanner.analyze_ticker("TEST", _df(prices), spy_ret_63=0.02, qqq_ret_63=0.02)
    assert res is not None
    assert res.above_50 and res.above_200
    assert res.target == pytest.approx(round(res.price * 1.05, 2))
    assert 0 <= res.technical <= 40
    assert res.price_score <= 65


def test_analyze_short_history_returns_none():
    assert scanner.analyze_ticker("X", _df([100] * 50), 0, 0) is None


def test_analyze_penny_excluded():
    assert scanner.analyze_ticker("X", _df([0.5] * 260), 0, 0) is None


def test_market_regime_classification():
    up = _df([100 * (1.002 ** i) for i in range(260)])
    down = _df([200 * (0.998 ** i) for i in range(260)])
    market = scanner.market_regime(up, up, vix=14.0, breadth=0.70)
    assert market["regime"] == "RISK-ON"
    off = scanner.market_regime(down, down, vix=30.0, breadth=0.30)
    assert off["regime"] == "RISK-OFF"


def test_report_builds_with_no_picks():
    regime = {"regime": "RISK-OFF", "vix": 28.0, "breadth": 0.3,
              "spy_trend": "below key MAs", "qqq_trend": "below key MAs"}
    msgs = scanner._build_report(regime, [], [], ["AAA: earnings in 1d"], 480)
    assert msgs and any("RISK-OFF" in m for m in msgs)
    assert any("No clean setups" in m for m in msgs)


def test_fundamentals_no_key_is_neutral():
    score, sector, company, notes = scanner.fmp_fundamentals("AAPL", None)
    assert score == 7.0 and sector is None and "unavailable" in notes[0]
