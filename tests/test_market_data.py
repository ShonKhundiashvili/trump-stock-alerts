"""Tests for the price-reaction (too-late) flag and research trade plan.

These exercise the pure math only — no network. yfinance is never called here.
"""

from __future__ import annotations

from market_data import Quote, already_ran, too_late_flag, trade_plan


def _q(price=100.0, atr=2.0, move_1d=0.0, move_5d=0.0) -> Quote:
    return Quote(ticker="TEST", price=price, atr=atr,
                 atr_pct=atr / price * 100, move_1d=move_1d, move_5d=move_5d)


# --- too_late_flag -------------------------------------------------------- #
def test_too_late_bullish_big_5d_run_flags_late():
    out = too_late_flag(_q(move_5d=0.08), "bullish")
    assert "may be late" in out
    assert "+8.0%" in out


def test_too_late_bullish_quiet_is_fresh():
    out = too_late_flag(_q(move_5d=0.01), "bullish")
    assert "fresh" in out
    assert "may be late" not in out


def test_too_late_bearish_big_drop_flags_late():
    out = too_late_flag(_q(move_5d=-0.09), "bearish")
    assert "may be late" in out


def test_too_late_bearish_quiet_is_fresh():
    assert "fresh" in too_late_flag(_q(move_5d=-0.01), "bearish")


def test_too_late_none_quote_is_empty():
    assert too_late_flag(None, "bullish") == ""


# --- trade_plan / position sizing ----------------------------------------- #
def test_trade_plan_bullish_sizing_math():
    # $10k account, 1% risk = $100. Entry 100, ATR 2 -> stop 98, risk/share $2 -> 50 shares.
    out = trade_plan(_q(price=100.0, atr=2.0), "bullish", account_size=10000, risk_pct=1.0)
    assert "entry ~$100" in out
    assert "stop $98" in out
    assert "~50 sh" in out
    assert "1% risk ($100)" in out


def test_trade_plan_target_is_five_percent_by_default():
    out = trade_plan(_q(price=100.0, atr=2.0), "bullish", 10000, 1.0)
    assert "target $105" in out
    assert "+5.0%" in out


def test_trade_plan_bearish_inverts_stop_and_target():
    out = trade_plan(_q(price=100.0, atr=2.0), "bearish", 10000, 1.0)
    assert "stop $102" in out      # stop above entry for a short
    assert "target $95" in out     # target below entry


def test_trade_plan_risk_pct_scales_share_count():
    one = trade_plan(_q(price=100.0, atr=2.0), "bullish", 10000, 1.0)
    two = trade_plan(_q(price=100.0, atr=2.0), "bullish", 10000, 2.0)
    assert "~50 sh" in one
    assert "~100 sh" in two


def test_trade_plan_zero_atr_returns_empty():
    assert trade_plan(_q(price=100.0, atr=0.0), "bullish", 10000, 1.0) == ""


def test_trade_plan_none_quote_returns_empty():
    assert trade_plan(None, "bullish", 10000, 1.0) == ""


def test_trade_plan_reports_reasonable_rr():
    # entry 100, stop 98 (risk 2), target 105 (reward 5) -> RR 2.5
    out = trade_plan(_q(price=100.0, atr=2.0), "bullish", 10000, 1.0)
    assert "RR 2.5" in out


# --- already_ran (the too-late suppression gate) -------------------------- #
def test_already_ran_bullish_big_5d_run_is_late():
    # DELL-style +61% over ~5d -> already ran past a 12% threshold.
    assert already_ran(_q(move_5d=0.61), "bullish", 12) is True


def test_already_ran_bullish_moderate_run_is_late():
    # HOOD-style +22% over ~5d.
    assert already_ran(_q(move_5d=0.22), "bullish", 12) is True


def test_already_ran_bullish_quiet_is_not_late():
    # INTC-style flat 5d move -> price hasn't run, gate does NOT suppress.
    assert already_ran(_q(move_5d=-0.001), "bullish", 12) is False


def test_already_ran_triggers_on_single_day_spike():
    assert already_ran(_q(move_1d=0.15, move_5d=0.02), "bullish", 12) is True


def test_already_ran_bearish_uses_downside():
    assert already_ran(_q(move_5d=-0.20), "bearish", 12) is True
    assert already_ran(_q(move_5d=0.20), "bearish", 12) is False


def test_already_ran_disabled_threshold_never_fires():
    assert already_ran(_q(move_5d=0.61), "bullish", 0) is False


def test_already_ran_none_quote_fails_open():
    assert already_ran(None, "bullish", 12) is False
