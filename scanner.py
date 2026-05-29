"""Weekly equity scanner — technical-first research screener.

Scans the S&P 500 + Nasdaq-100 universe once per week and surfaces 5-10
high-conviction names with a realistic ~5% upside setup over 1-2 weeks. It is
RESEARCH ONLY — not financial advice, no guarantees; the 5% target is a
potential scenario, not a prediction.

Scoring (0-100): Technical 40 + Relative strength 15 + Risk/reward 10 (all from
price data) + Fundamentals 15 + Catalyst/news 20 (finalists only, via FMP +
news). Deterministic / data-driven theses (no LLM).

Data: yfinance for prices (chunked + resilient); FMP for finalist fundamentals
and analyst revisions (needs FMP_API_KEY; degrades gracefully without it).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import config_loader

logger = logging.getLogger(__name__)

DISCLAIMER = "Research only — not financial advice. Targets are potential scenarios, not predictions."
TARGET_PCT = 0.05  # ~5% upside scenario


# --------------------------------------------------------------------------- #
# indicators (pure, testable)
# --------------------------------------------------------------------------- #
def sma(series, n):
    return series.rolling(n).mean()


def rsi(series, n: int = 14):
    delta = series.diff()
    up = delta.clip(lower=0).rolling(n).mean()
    down = (-delta.clip(upper=0)).rolling(n).mean()
    rs = up / down            # down==0 -> inf -> RSI 100 (all-gains window)
    return 100 - 100 / (1 + rs)


def macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    line = ema_fast - ema_slow
    sig = line.ewm(span=signal, adjust=False).mean()
    return line, sig, line - sig


def atr(high, low, close, n: int = 14):
    import pandas as pd
    prev_close = close.shift()
    tr = pd.concat([(high - low), (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def pct_return(series, lookback: int) -> float:
    if len(series) <= lookback:
        return 0.0
    try:
        return float(series.iloc[-1] / series.iloc[-1 - lookback] - 1.0)
    except (ZeroDivisionError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
# per-ticker technical analysis
# --------------------------------------------------------------------------- #
@dataclass
class TechResult:
    ticker: str
    price: float
    setup: str
    technical: float          # 0-40
    rel_strength: float       # 0-15
    risk_reward: float        # 0-10
    rr_ratio: float
    target: float
    stop: float
    support: float
    resistance: float
    rsi: float
    atr_pct: float
    above_50: bool
    above_200: bool
    dollar_vol: float
    feasible_5pct: bool
    notes: List[str] = field(default_factory=list)

    @property
    def price_score(self) -> float:
        return self.technical + self.rel_strength + self.risk_reward


def analyze_ticker(ticker, df, spy_ret_63, qqq_ret_63) -> Optional[TechResult]:
    """Compute technicals + setup + the price-based score (0-65). None if unusable."""
    try:
        df = df.dropna(subset=["Close"])
        if len(df) < 210:
            return None
        close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
        price = float(close.iloc[-1])
        if price <= 1:
            return None  # penny

        s20, s50, s200 = sma(close, 20), sma(close, 50), sma(close, 200)
        a = float(atr(high, low, close, 14).iloc[-1])
        r = float(rsi(close, 14).iloc[-1])
        _, _, hist = macd(close)
        macd_hist = float(hist.iloc[-1])
        macd_rising = float(hist.iloc[-1]) > float(hist.iloc[-2])
        avg_vol20 = float(vol.tail(20).mean())
        last_vol = float(vol.iloc[-1])
        atr_pct = a / price if price else 0.0
        dollar_vol = avg_vol20 * price

        ma20, ma50, ma200 = float(s20.iloc[-1]), float(s50.iloc[-1]), float(s200.iloc[-1])
        above_20, above_50, above_200 = price > ma20, price > ma50, price > ma200

        recent_high = float(high.tail(60).max())
        recent_low = float(low.tail(40).min())
        range_pct = (recent_high - recent_low) / price if price else 1.0

        # --- setup detection ---
        uptrend = above_50 and above_200 and ma50 >= ma200
        near_high = price >= recent_high * 0.97
        vol_spike = avg_vol20 > 0 and last_vol > 1.3 * avg_vol20
        tight = (float(high.tail(20).max()) - float(low.tail(20).min())) / price < 0.09

        setup, setup_score = "none", 2.0
        if uptrend and near_high and vol_spike:
            setup, setup_score = "breakout", 6.0
        elif uptrend and 40 <= r <= 58 and abs(price - ma20) / price < 0.04:
            setup, setup_score = "pullback", 6.0
        elif above_200 and tight and 45 <= r <= 65:
            setup, setup_score = "consolidation/base", 4.0
        elif r < 35 and price > ma20:
            setup, setup_score = "reversal", 3.0

        # --- technical score (0-40) ---
        trend = (5 if above_200 else 0) + (5 if above_50 else 0) + \
                (3 if above_20 else 0) + (3 if ma50 >= ma200 else 0)
        if 50 <= r <= 65:
            mom = 6
        elif 45 <= r <= 70:
            mom = 3
        else:
            mom = 0
        mom += (4 if macd_hist > 0 else 0) + (2 if macd_rising else 0)
        volsc = 6 if vol_spike else (3 if avg_vol20 and last_vol > avg_vol20 else 0)
        technical = min(40.0, trend + min(mom, 12) + volsc + setup_score)

        # --- relative strength (0-15) ---
        ret63 = pct_return(close, 63)
        beats_spy, beats_qqq = ret63 > spy_ret_63, ret63 > qqq_ret_63
        if beats_spy and beats_qqq:
            rs = 15.0
        elif beats_spy or beats_qqq:
            rs = 10.0
        elif ret63 > 0:
            rs = 5.0
        else:
            rs = 2.0

        # --- target / stop / risk-reward ---
        # Target is the ~5% scenario; stop is 1R = 1 ATR (a clean setup needs
        # risk < reward, so RR>=1.5 => ATR% <= ~3.3%, i.e. lower-volatility names).
        target = round(price * (1 + TARGET_PCT), 2)
        support = max(recent_low, ma50 if above_50 else recent_low)
        stop = round(price - 1.0 * a, 2)
        # If a nearby support sits just under price, a stop just below it is even
        # cleaner (smaller risk) — use it when it tightens risk without being noise.
        if price * 0.985 >= support * 0.995 >= price - 1.0 * a:
            stop = round(support * 0.995, 2)
        rr_ratio = (target - price) / (price - stop) if price > stop else 0.0
        if rr_ratio >= 2.5:
            rrsc = 10.0
        elif rr_ratio >= 2:
            rrsc = 8.0
        elif rr_ratio >= 1.5:
            rrsc = 6.0
        elif rr_ratio >= 1:
            rrsc = 3.0
        else:
            rrsc = 0.0

        # 5% realistic over ~2 weeks: ~ATR*sqrt(10 trading days); avoid wild names.
        two_week_move = atr_pct * math.sqrt(10)
        feasible = (two_week_move >= 0.04) and (atr_pct <= 0.08) and range_pct < 0.40

        return TechResult(
            ticker=ticker, price=round(price, 2), setup=setup,
            technical=round(technical, 1), rel_strength=rs, risk_reward=rrsc,
            rr_ratio=round(rr_ratio, 2), target=target, stop=stop,
            support=round(support, 2), resistance=round(recent_high, 2),
            rsi=round(r, 1), atr_pct=round(atr_pct * 100, 2),
            above_50=above_50, above_200=above_200, dollar_vol=dollar_vol,
            feasible_5pct=feasible,
        )
    except Exception as exc:  # noqa: BLE001 - never let one ticker break the scan
        logger.debug("analyze_ticker(%s) failed: %s", ticker, exc)
        return None


# --------------------------------------------------------------------------- #
# data fetch (yfinance, chunked + resilient)
# --------------------------------------------------------------------------- #
def _yf_symbol(t: str) -> str:
    return t.replace(".", "-").upper()


def _prepare_yfinance(yf) -> None:
    """Make yfinance work in a restricted (launchd agent) context.

    A macOS launchd agent runs with a minimal environment: it can't spawn
    curl's threaded DNS resolver ("getaddrinfo() thread failed to start") and
    can't open yfinance's default platform tz-cache ("unable to open database
    file"). Pinning the cache to an app-local writable dir fixes the latter;
    fetch_prices uses threads=False (sequential, main-thread resolver) for the
    former. Both are idempotent and harmless in a normal shell / CI too.
    """
    try:
        cache_dir = config_loader.DATA_DIR / "yf-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(cache_dir))
    except Exception as exc:  # noqa: BLE001 - cache pinning is best-effort
        logger.debug("Could not set yfinance tz cache location: %s", exc)


def fetch_prices(tickers: List[str], period: str = "1y", chunk: int = 80) -> Dict[str, "object"]:
    """Download daily OHLCV for many tickers; returns {ticker: DataFrame}."""
    import yfinance as yf
    _prepare_yfinance(yf)
    out: Dict[str, object] = {}
    syms = [_yf_symbol(t) for t in tickers]
    for i in range(0, len(syms), chunk):
        part = syms[i:i + chunk]
        for attempt in range(2):
            try:
                # threads=False: download on the calling thread (sync DNS
                # resolver). threads=True spawns one worker per symbol and each
                # curl request spawns its own resolver thread, which fails in a
                # launchd agent's restricted context ("getaddrinfo() thread
                # failed to start"). Sequential is ~3 min for the full universe —
                # fine for a once-daily scan — and works everywhere.
                data = yf.download(part, period=period, interval="1d", group_by="ticker",
                                   auto_adjust=True, threads=False, progress=False)
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("yf.download chunk %d failed (try %d): %s", i, attempt, exc)
                data = None
                time.sleep(2)
        if data is None:
            continue
        for sym in part:
            try:
                df = data[sym] if len(part) > 1 else data
                if df is not None and not df.dropna(how="all").empty:
                    out[sym] = df
            except Exception:  # noqa: BLE001
                continue
        time.sleep(1)  # be gentle with Yahoo
    return out


# --------------------------------------------------------------------------- #
# fundamentals (FMP — finalists only; degrades without a key)
# --------------------------------------------------------------------------- #
# FMP retired the legacy /api/v3 path endpoints (403 for accounts created after
# 2025-08-31) in favour of the "stable" API: a flat base + ?symbol= query form.
FMP = "https://financialmodelingprep.com/stable"


def fmp_fundamentals(symbol: str, api_key: Optional[str]):
    """Return (score 0-15, sector, company, notes[]). Neutral when no key / data."""
    if not api_key:
        return 7.0, None, None, ["fundamentals unavailable (no FMP key) — neutral"]
    import requests
    sector, company, notes, score = None, None, [], 0.0
    try:
        def _get(ep, **extra):
            r = requests.get(f"{FMP}/{ep}",
                             params={"symbol": symbol, "apikey": api_key, **extra}, timeout=15)
            data = r.json()
            return data if isinstance(data, list) else []

        prof = _get("profile")
        if prof:
            sector = prof[0].get("sector")
            company = prof[0].get("companyName")
        ratios = _get("ratios-ttm")
        grow = _get("income-statement-growth", limit=1)
        rt = ratios[0] if ratios else {}
        gr = grow[0] if grow else {}
        rev_g = gr.get("growthRevenue")
        ni_g = gr.get("growthNetIncome")
        net_m = rt.get("netProfitMarginTTM")
        de = rt.get("debtToEquityRatioTTM")          # renamed from debtEquityRatioTTM
        fcf = rt.get("freeCashFlowPerShareTTM")
        # peRatioTTM no longer exists on ratios-ttm; derive P/E from price ÷ EPS.
        price = prof[0].get("price") if prof else None
        eps = rt.get("netIncomePerShareTTM")
        pe = (price / eps) if (price and eps and eps > 0) else None
        if rev_g and rev_g > 0:
            score += 3; notes.append(f"rev growth {rev_g*100:.0f}%")
        if ni_g and ni_g > 0:
            score += 3; notes.append("earnings growing")
        if net_m and net_m > 0:
            score += 2 + (1 if net_m > 0.15 else 0)
        if de is not None and de < 1:
            score += 2; notes.append("low debt")
        if fcf and fcf > 0:
            score += 2; notes.append("positive FCF")
        if pe and 0 < pe < 40:
            score += 2
        return min(score, 15.0), sector, company, (notes or ["fundamentals mixed/neutral"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("FMP fundamentals %s failed: %s", symbol, exc)
        return 7.0, sector, company, ["fundamentals unavailable — neutral"]


def fmp_recent_upgrade(symbol: str, api_key: Optional[str]) -> Optional[str]:
    if not api_key:
        return None
    import requests
    try:
        r = requests.get(f"{FMP}/grades",
                         params={"symbol": symbol, "apikey": api_key, "limit": 5}, timeout=15)
        g = r.json()
        for row in (g if isinstance(g, list) else []):
            ng = (row.get("newGrade") or "").lower()
            if any(k in ng for k in ("buy", "overweight", "outperform", "strong")):
                return f"{row.get('gradingCompany')} → {row.get('newGrade')}"
    except Exception:  # noqa: BLE001
        pass
    return None


def days_to_earnings(symbol: str) -> Optional[int]:
    """Trading-ish days until next earnings (yfinance). None if unknown."""
    import yfinance as yf
    from datetime import datetime, timezone
    try:
        cal = yf.Ticker(symbol).calendar
        dt = None
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            dt = ed[0] if isinstance(ed, list) and ed else ed
        if dt is None:
            return None
        d = dt if hasattr(dt, "year") else None
        if d is None:
            return None
        delta = (d - datetime.now().date()) if hasattr(d, "year") and not hasattr(d, "hour") \
            else (d.date() - datetime.now().date())
        return delta.days
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# market regime
# --------------------------------------------------------------------------- #
def _trend(df) -> str:
    try:
        c = df["Close"].dropna()
        price = float(c.iloc[-1])
        up = price > float(sma(c, 50).iloc[-1]) and price > float(sma(c, 200).iloc[-1])
        return "uptrend" if up else "below key MAs"
    except Exception:  # noqa: BLE001
        return "unknown"


def market_regime(spy_df, qqq_df, vix: Optional[float], breadth: float) -> dict:
    spy_t, qqq_t = _trend(spy_df), _trend(qqq_df)
    spy_up, qqq_up = spy_t == "uptrend", qqq_t == "uptrend"
    if (vix and vix > 24) or not spy_up or breadth < 0.40:
        regime = "RISK-OFF"
    elif spy_up and qqq_up and (vix is None or vix < 20) and breadth > 0.55:
        regime = "RISK-ON"
    else:
        regime = "NEUTRAL"
    return {"regime": regime, "vix": vix, "breadth": breadth,
            "spy_trend": spy_t, "qqq_trend": qqq_t}


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
@dataclass
class Pick:
    tech: TechResult
    total: float
    fund: float
    catalyst: float
    sector: Optional[str]
    company: Optional[str]
    fnotes: List[str]
    cnotes: List[str]
    dte: Optional[int]


def run_scan(settings, alerter) -> dict:
    cfg = config_loader.load_scanner()
    universe = sorted(set(config_loader.load_priority_tickers().get("ALL", [])))
    if not universe:
        logger.warning("Scanner: empty universe")
        return {"picks": 0}
    logger.info("Scanner: fetching prices for %d names + benchmarks", len(universe))
    data = fetch_prices(universe + ["SPY", "QQQ"])
    vix_data = fetch_prices(["^VIX"])
    vix = None
    try:
        vix = float(vix_data["^VIX"]["Close"].dropna().iloc[-1])
    except Exception:  # noqa: BLE001
        pass

    spy_df, qqq_df = data.get("SPY"), data.get("QQQ")
    spy_ret = pct_return(spy_df["Close"].dropna(), 63) if spy_df is not None else 0.0
    qqq_ret = pct_return(qqq_df["Close"].dropna(), 63) if qqq_df is not None else 0.0

    results: List[TechResult] = []
    for t in universe:
        df = data.get(_yf_symbol(t))
        if df is None:
            continue
        r = analyze_ticker(t, df, spy_ret, qqq_ret)
        if r:
            results.append(r)
    if not results:
        logger.warning("Scanner: no analyzable results (data fetch likely failed)")
        return {"picks": 0, "error": "no data"}

    breadth = sum(1 for r in results if r.above_50) / len(results)
    regime = market_regime(spy_df, qqq_df, vix, breadth)

    min_dv = cfg.get("min_dollar_volume", 20_000_000)
    min_rr = cfg.get("min_rr", 1.5)
    min_score = cfg.get("min_score", 62)
    candidates = [r for r in results if r.feasible_5pct and r.dollar_vol >= min_dv
                  and r.rr_ratio >= min_rr and r.setup != "none"]
    candidates.sort(key=lambda r: r.price_score, reverse=True)
    finalists = candidates[: cfg.get("max_finalists", 18)]

    picks: List[Pick] = []
    avoid: List[str] = []
    fmp_key = settings.fmp_api_key
    for r in finalists:
        fund, sector, company, fnotes = fmp_fundamentals(r.ticker, fmp_key)
        dte = days_to_earnings(_yf_symbol(r.ticker))
        cnotes = []
        if dte is not None and 0 <= dte <= 3:
            avoid.append(f"{r.ticker}: earnings in ~{dte}d (skipped per rules)")
            continue
        catalyst = 8.0
        up = fmp_recent_upgrade(r.ticker, fmp_key)
        if up:
            catalyst += 6; cnotes.append(f"recent upgrade: {up}")
        if dte is not None and 4 <= dte <= 14:
            catalyst += 4; cnotes.append(f"earnings in ~{dte}d (catalyst)")
        catalyst = min(catalyst, 20.0)
        if dte is not None:
            cnotes.append(f"next earnings ~{dte}d out")
        total = r.price_score + fund + catalyst
        picks.append(Pick(r, round(total, 1), fund, catalyst, sector, company,
                          fnotes, cnotes, dte))

    picks.sort(key=lambda p: p.total, reverse=True)
    qualified = [p for p in picks if p.total >= min_score]

    # Regime caps how many we'll surface (fewer when weak — don't force picks).
    cap = {"RISK-ON": 10, "NEUTRAL": 6, "RISK-OFF": 3}[regime["regime"]]
    top = qualified[:cap]
    watchlist = [p for p in picks if p not in top and p.total >= min_score - 8][:6]

    msgs = _build_report(regime, top, watchlist, avoid, len(results))
    sent = 0
    for m in msgs:
        if alerter.send_notice(m, channel="weekly"):
            sent += 1
        time.sleep(0.4)
    logger.info("Scanner done: %d picks, %d messages sent, regime=%s",
                len(top), sent, regime["regime"])
    return {"picks": len(top), "regime": regime["regime"], "messages": sent}


# --------------------------------------------------------------------------- #
# report (data-driven, plain text + light HTML)
# --------------------------------------------------------------------------- #
def _e(s) -> str:
    import html
    return html.escape(str(s))


def _build_report(regime, top, watchlist, avoid, universe_n) -> List[str]:
    """Compact, trade-ready format: one pick per 2 lines, just the numbers."""
    vix = f"{regime['vix']:.1f}" if regime["vix"] else "n/a"
    lines = [
        f"📊 <b>Daily Scan — {regime['regime']}</b> · {len(top)} picks · "
        f"VIX {vix} · breadth {regime['breadth']*100:.0f}%",
    ]
    if not top:
        lines.append("No clean setups met the bar this week — staying flat is a position.")
    for i, p in enumerate(top, 1):
        t = p.tech
        er = f"ER ~{p.dte}d" if p.dte is not None else "ER n/a"
        lines.append(
            f"\n{i}. <b>{_e(t.ticker)}</b> · {t.setup} · score {p.total:.0f}\n"
            f"   entry ${t.price:g} → tgt ${t.target:g} (+5%) · stop ${t.stop:g} · RR {t.rr_ratio:g}\n"
            f"   sup ${t.support:g} · res ${t.resistance:g} · RSI {t.rsi:g} · "
            f"ATR {t.atr_pct:g}% · {er}"
        )
    if watchlist:
        lines.append("\n👀 Watchlist: " +
                     ", ".join(f"{_e(p.tech.ticker)}({p.total:.0f})" for p in watchlist))
    if avoid:
        lines.append("🚫 Avoid: " + "; ".join(_e(a) for a in avoid[:6]))
    lines.append(f"\n<i>{DISCLAIMER}</i>")

    # One message, split only if it would exceed Telegram's limit.
    text = "\n".join(lines)
    if len(text) <= 3800:
        return [text]
    out, buf = [], ""
    for ln in lines:
        if len(buf) + len(ln) + 1 > 3800:
            out.append(buf); buf = ""
        buf += ln + "\n"
    if buf:
        out.append(buf)
    return out
