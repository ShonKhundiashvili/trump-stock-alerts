# Stock Breakout Tracker — Design Spec

**Date:** 2026-05-29
**Status:** Approved (design) — pending spec review
**Author:** Claude Code session

## 1. Purpose

A reusable Claude skill that scans a configurable stock universe every weekday
near the US market open, identifies names with **short-term 5–10% upside
potential** from technical analysis, classifies each by setup, ranks them with a
0–100 score, and produces a daily markdown report (also posted compactly to
Telegram).

Research only. Every report ends with:
> "This is not financial advice. These are technical setups for research only. Always manage risk."

## 2. Scope & relationship to existing code

This is a **new skill that reuses the project's hardened engine** — it does NOT
duplicate data/indicator/Telegram plumbing, and it runs **alongside** (not
replacing) the existing `scanner.py` Daily Scan.

Reuse points:
- `scanner.fetch_prices` — launchd-safe, chunked yfinance daily OHLCV.
- `scanner.sma / rsi / macd / atr / pct_return` — base indicators.
- `scanner.fmp_fundamentals` — sector + market cap (FMP stable API).
- `config_loader.load_priority_tickers` — S&P 500 + Nasdaq 100 universe.
- `config_loader.load_settings` — FMP key, Telegram creds, account size, risk %.
- `config_loader.load_topics` + `telegram_alerts.TelegramAlerter` — posting,
  honoring the room invariant (`has_dedicated_route`) added earlier this session.

Out of scope (YAGNI): intraday/60m Strat timeframe, options data, live order
routing, backtesting harness, ML scoring.

## 3. Package layout

```
.claude/skills/stock-breakout-tracker/
  SKILL.md                  # Claude skill manifest (frontmatter + how/when to use)
  README.md                 # human docs: run, schedule, config, keys, example
  config.example.json       # config template (copy to config.json)
  breakout_tracker/
    __init__.py
    config.py               # load skill config.json; merge with project config_loader
    data_fetcher.py         # DataFetcher: daily OHLCV (via scanner.fetch_prices)
                            #   + resample to weekly/monthly; pluggable source iface
    indicators.py           # reuse scanner indicators + 20MA distance, swing
                            #   highs/lows, avg volume, $-volume, ATR%
    pattern_detection.py    # 7 detectors -> PatternSignal(name, detected, quality, levels)
    strat_signals.py        # inside/outside bars, 2u/2d, reversal, D+W+M continuity
    mean_reversion.py       # 20-MA distance + reversal-candle confirmation
    scoring.py              # 10-factor 0-100 score + classification buckets
    report_generator.py     # full markdown report + compact Telegram message
    scheduler.py            # weekday guard for scheduled mode
    __main__.py             # CLI entry: manual run + flags
    tests/
      __init__.py
      test_pattern_detection.py
      test_scoring.py
      test_strat_signals.py
      conftest.py           # synthetic OHLCV fixture builders (no network)
  deploy/
    run.sh                  # cd project, set PYTHONPATH, run module
    com.trumpstockalerts.breakout.plist  # launchd Mon-Fri 16:25 IDT
```

The package imports project modules (scanner, config_loader, telegram_alerts) by
running from the project root with `PYTHONPATH` including both the project root
and the skill package's parent dir (set in `run.sh` and documented in README).

## 4. Data flow

```
universe (config: priority_tickers + filters)
  -> DataFetcher: daily OHLCV (chunked) ; resample -> weekly, monthly
  -> per ticker:
       indicators (20/50 MA, RSI, MACD, ATR%, avg vol, $-vol, swings)
       pattern_detection (7 detectors)
       strat_signals (D/W/M inside/outside/2u-2d/reversal + continuity)
       mean_reversion (distance from 20MA + reversal candle)
       targets/stops (measured move or 20MA reclaim, capped at resistance,
                      bounded to 5-10% band; stop from support/swing/ATR)
  -> scoring (0-100) -> classification -> rank
  -> top-N + watchlist + avoid
  -> report_generator: write data/reports/breakouts-YYYY-MM-DD.md
                       + post compact to Telegram (configurable topic)
```

## 5. Component contracts

### data_fetcher.py
- `class DataFetcher` with `daily(tickers) -> dict[str, DataFrame]`,
  `multi_timeframe(ticker) -> {"D":df,"W":df,"M":df}` (W/M via pandas resample of
  daily OHLCV: open=first, high=max, low=min, close=last, volume=sum).
- Default impl wraps `scanner.fetch_prices`. The class is the **pluggable
  interface** — a future source implements the same methods.

### indicators.py
- Thin re-exports of `scanner.sma/rsi/macd/atr` plus:
  `ma_distance_pct(price, ma)`, `avg_volume(df, n)`, `dollar_volume(df, n)`,
  `atr_pct(df, n)`, `swing_highs(df, lookback)`, `swing_lows(df, lookback)`,
  `trend_strength(df)` (e.g. slope of 50MA + 20>50>price stacking).

### pattern_detection.py
- `@dataclass PatternSignal { name:str, detected:bool, quality:float (0-1),
  key_level:float|None, target_hint:float|None, notes:str }`
- Detectors (each `detect_*(df) -> PatternSignal`):
  `cup_and_handle`, `head_and_shoulders` (+ inverse), `bull_flag`,
  `consolidation_breakout`, `higher_highs_lows`, `support_resistance_breakout`,
  `volume_breakout`.
- `detect_all(df) -> list[PatternSignal]` runs the suite; the highest-quality
  bullish signal becomes the primary "setup type".
- Detection uses swing pivots + simple geometric rules (documented heuristics),
  not ML. Each detector is independently unit-testable on synthetic OHLCV.

### strat_signals.py
- `bar_type(prev, cur) -> "1"|"2u"|"2d"|"3"` (inside / 2-up / 2-down / outside).
- `continuity(df) -> {"2u_continuation":bool, "reversal":bool, ...}` per timeframe.
- `timeframe_alignment(d,w,m) -> float (0-1)` — fraction of D/W/M agreeing bullish.
- `strat_confidence(d,w,m) -> (score 0-1, notes)` — used as a confidence factor,
  never the sole decision factor.

### mean_reversion.py
- `mean_reversion_signal(df) -> { distance_pct, below_ma:bool, reversal:bool,
  candidate:bool, est_upside_pct }` — flags stocks significantly below the 20 MA
  showing reversal candles (bullish engulfing/hammer + rising volume); estimates
  upside as the move back toward the 20 MA, bounded to 5–10%.

### scoring.py
- `score(features) -> (int 0-100, breakdown:dict)` with weights:
  Pattern 20 · Volume 12 · Trend 12 · Strat 12 · MA proximity 10 · R/R 10 ·
  Distance-to-target 8 · Volatility fit 6 · Liquidity 6 · Sector 4 (= 100).
  Each sub-score is a documented 0–1 (or 0–weight) function; deterministic.
- `classify(features, score) -> Classification` (enum):
  HIGH_CONFIDENCE_BREAKOUT, CUP_AND_HANDLE, MEAN_REVERSION_20MA,
  STRAT_BULLISH_CONTINUATION, EARLY_REVERSAL, WATCHLIST_ONLY, AVOID.
  Rules: dominant signal selects the bucket; score thresholds gate
  high-confidence vs watchlist vs avoid; below `min_confidence` => AVOID/filtered.

### report_generator.py
- `Candidate` dataclass holds every report field (ticker, price, setup,
  classification, reason, 20MA distance, support, resistance, target, upside %,
  stop, R/R, score, notes).
- `render_markdown(candidates, session, date) -> str` — exact format from the
  request (title "Daily Breakout Tracker", per-stock fields, disclaimer footer).
- `render_telegram(candidates, ...) -> str` — compact HTML for the topic.

### scheduler.py
- `is_trading_weekday(date) -> bool` (Mon–Fri; no US-holiday calendar in v1 —
  documented limitation).
- Scheduled mode skips weekends; manual `--run` ignores the guard.

### __main__.py (CLI)
- `python -m breakout_tracker --run` plus flags:
  `--watchlist a,b,c` | `--watchlist-file path`, `--sector "Technology"`
  (repeatable / comma list), `--market-cap-min N`, `--min-dollar-volume N`,
  `--min-score N`, `--top N`, `--no-telegram`, `--date YYYY-MM-DD`
  (override "today" for testing), `--out PATH`, `--scheduled` (apply weekend guard).
- Config precedence: CLI flag > config.json > built-in default.

## 6. Configuration (config.json)

```json
{
  "universe": "priority_tickers",        // or "watchlist"
  "watchlist": [],                        // used when universe == "watchlist"
  "sectors_include": [],                  // empty = all
  "sectors_exclude": [],
  "market_cap_min": 0,                    // USD; 0 = no filter (needs FMP)
  "min_dollar_volume": 20000000,          // liquidity floor
  "min_confidence": 60,                   // drop below this
  "top_n": 12,
  "enable_telegram": true,
  "telegram_channel": "weekly",           // existing Daily Scan topic (id 38)
  "report_dir": "data/reports"
}
```

Sector/market-cap filters require FMP (now configured). When FMP is absent those
filters no-op with a logged note (graceful degradation, matching scanner.py).

## 7. Output format

Markdown file `data/reports/breakouts-YYYY-MM-DD.md`:

```
# Daily Breakout Tracker

Date: 2026-05-29
Market session: Pre-open (US)

## Top Candidates

Ticker: XYZ
Setup: Cup and handle breakout
Classification: High-confidence breakout
Reason: ...
Current price: $50
Distance from 20 MA: +2.1%
Key support: $47.50
Key resistance: $50.20
Target: $55
Estimated upside: 10%
Stop-loss: $47.80
Risk/reward: 2.3
Confidence score: 84/100
Notes: ...

(… more candidates …)

## Watchlist
(name — one-line reason — score)

---
This is not financial advice. These are technical setups for research only. Always manage risk.
```

Telegram version: compact — one block per top candidate (ticker, setup, score,
price→target, upside %, stop), plus the disclaimer.

## 8. Scheduling

- `deploy/com.trumpstockalerts.breakout.plist`: launchd `StartCalendarInterval`
  for Weekday 1–5 at 16:25 local (= 9:25 ET, pre-open). One-shot (no KeepAlive,
  no RunAtLoad). Logs to `data/breakout.out.log` / `data/breakout.err.log`.
- `deploy/run.sh`: `cd` to project, set `PYTHONPATH`, exec
  `.venv/bin/python -m breakout_tracker --run --scheduled`.
- Manual run: same command without `--scheduled` (works any day), or via the
  skill from Claude.
- Installation documented in README (cp to ~/Library/LaunchAgents; launchctl load).
- No double-post risk with the existing scan: that runs 16:30; this runs 16:25.

## 9. Testing

- `conftest.py` builds deterministic synthetic OHLCV DataFrames for known shapes:
  a clean cup-and-handle, a bull flag, an inside bar, a 2u continuation, a stock
  8% below its 20 MA with a reversal candle.
- `test_pattern_detection.py`: each detector fires on its shape and stays quiet
  on noise; quality scores ordered sensibly.
- `test_strat_signals.py`: bar_type classification + continuity + alignment.
- `test_scoring.py`: weights sum to 100; monotonicity (better inputs => higher
  score); classification thresholds; min_confidence filtering.
- All pure, no network. Wired into the existing `pytest` run (`pytest.ini`).

## 10. Deliverables (on completion)

- Files created/modified list.
- How to run manually (CLI examples).
- How the Mon–Fri schedule works (launchd install).
- Example output report (generated from a real or fixture run).
- API keys / data sources needed (yfinance free; FMP optional for sector/cap —
  already configured).

## 11. Risks & limitations

- Pattern detection is heuristic; will have false positives/negatives. Mitigated
  by the composite score + classification (patterns are one factor, not gospel).
- No US market-holiday calendar in v1 — runs Mon–Fri regardless of holidays
  (documented; report still valid on prior-session bars).
- yfinance rate limits / occasional gaps — inherited resilience from
  `scanner.fetch_prices` (chunking, retries, sequential resolver).
- The Strat is daily/weekly/monthly only — no intraday continuity in v1.
```
