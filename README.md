# trump-stock-alerts

A Telegram bot that monitors **public** Trump-related sources for mentions of
public companies/tickers with buy/investment language, and sends you a **concise
alert with the source link, a confidence score, and a verification verdict**.

You classify each alert from Telegram with inline buttons, and the bot **learns**
your preferences (local, rule-based, transparent) to tune future alerts.

> Classifies information only. **No auto-trading. No financial advice.** No
> bypassing logins/paywalls/rate limits. Always verify via the source link.

---

## What it does

- Polls sources (RSS/transcripts, Google-News keyword searches, optional X) for
  company/ticker mentions — dynamically, against a ~12k US-ticker universe (not a
  hardcoded list), with S&P 500 / Nasdaq-100 used as a precision/trust signal.
- Detects buy/ownership/praise/performance language **and bearish signals**
  (attacks, tariffs, investigations, negative performance) — each alert is tagged
  📈 bullish or 📉 bearish — and scores it 0–100.
- Tracks Trump **and** the administration + a few other market-movers (Fed,
  Treasury, etc.), plus official catalysts like **federal contract awards**
  (USAspending.gov).
- **Recency:** only recent posts alert; stale/old items are stored but suppressed.
- **Cross-source verification:** flags single-source/unverified "Breaking…" claims;
  HIGH needs a primary source or multiple independent sources.
- **Human-in-the-loop:** inline Telegram buttons + commands; learns from your labels.
- Stores everything in SQLite and de-dupes (per item, and across sources).

## Project layout

```
main.py                 # polling loop + feedback receiver thread
detector.py             # cashtags, tickers, NER, phrases, proximity scoring
ticker_resolver.py      # name/ticker -> symbol (watchlist + universe + fuzzy)
alert_policy.py         # provenance/verification + timestamp parsing
feedback_learning.py    # alert scoring + send/suppress decision (rule-based)
feedback_bot.py         # Telegram callbacks + commands (long-poll / drain)
telegram_alerts.py      # concise alert formatting + inline buttons
db.py  models.py  config_loader.py
config/  sources.json  watchlist.json  phrases.json
         source_priority.json  priority_tickers.json  alerting.json
data/stock_universe.csv          scripts/  sources/  tests/
```

## Setup

Python 3.11+.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm     # enables dynamic company NER
cp .env.example .env                          # then fill in tokens
```

`.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (required); `X_BEARER_TOKEN`,
`OPENAI_API_KEY`/`ANTHROPIC_API_KEY` (optional); `POLL_SECONDS`, `DATABASE_PATH`,
`MIN_ALERT_CONFIDENCE`, `ENABLE_TELEGRAM_FEEDBACK`.

- **Telegram bot:** message `@BotFather` → `/newbot` → copy token.
- **Chat ID:** message your bot, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id`.
- **X token (optional):** X API v2 bearer. Reading timelines needs a paid tier;
  leave blank to skip X (RSS + Google-News cover Trump well for free).

## Configuration (`config/`)

- **sources.json** — feeds/searches by tier (PRIMARY transcripts, SECONDARY news,
  optional SOCIAL). `news_search.queries` are Google-News keyword searches.
  Also supports **YouTube** (official Data API + optional public captions, to catch
  spoken/video remarks — needs `YOUTUBE_API_KEY`), **GDELT** (free, keyless news
  index — on by default), **NewsAPI** (needs `NEWSAPI_KEY`), **USAspending**
  (free, keyless — recent large federal contract awards to public companies), and
  **Polymarket + Kalshi** prediction markets (free, keyless — stock/crypto/M&A
  markets only, e.g. "Tesla & SpaceX merge", routed to a `predictions` channel).
  More independent sources → stronger cross-source verification.
- **watchlist.json** — priority aliases/overrides for common companies.
- **phrases.json** — buy (HIGH) vs praise (MEDIUM) phrase lists.
- **source_priority.json** — maps a source to PRIMARY / SECONDARY / SOCIAL_RUMOR.
- **priority_tickers.json** — S&P 500 + Nasdaq-100 (trust signal; off-index
  tickers still detected, but need stronger evidence).
- **alerting.json** — gating knobs:
  `min_alert_score` (60), `max_age_hours` (48), `send_low_confidence`,
  `send_social_rumor` / `social_rumor_min_score`, `social_requires_corroboration`,
  `penalize_uncorroborated`, `respect_muted_sources/companies`, and the
  cross-source verification gate: `require_corroboration` (hold non-primary
  claims until confirmed), `min_independent_sources` (2), `ticker_cooldown_hours`
  (6, one alert per ticker per window).

**Verification before alerting:** Trump's own source (PRIMARY — his video
captions / Truth Social / White House transcript) alerts on its own. Any other
claim is **held** (`awaiting_corroboration`) until a PRIMARY source *or* ≥
`min_independent_sources` independent sources report the same ticker — so a lone
"Breaking: Trump said buy X" never fires unless it's actually confirmed.

Update the ticker universe with `python scripts/update_stock_universe.py`
(falls back to the bundled CSV if offline).

## Human-in-the-loop feedback & learning

Every alert has inline buttons; tapping one stores your label and adjusts future
scoring. Buttons: **✅ Useful · ❌ Fake · ⚠️ Not Useful · 🧵 Needs Context ·
🚫 Mute Source · 🔕 Mute Company · 📈 Too Late · 🧪 Training Example**.

How it affects scoring (deterministic, 0–100): base by confidence; ±provenance;
+ direct-buy phrase; − ambiguous/uncorroborated; ± your learned source/company/
phrase quality. Mutes and the score thresholds in `alerting.json` decide what
sends — suppressed alerts are still stored with a reason. It never trains a model
or edits code/config on its own.

**Commands** (your chats only): `/stats`, `/mutes`, `/unmute_source <s>`,
`/unmute_company <ticker>`, `/recent`, `/help`.

**Separate channels (optional):** route categories to different Telegram chats so
Trump's own announcements, market/other-politician news, contracts, and social
chatter don't mix. `config/channels.json` maps sources to channels (`trump`,
`markets`, `contracts`, `social`); each channel's chat id comes from
`TELEGRAM_CHAT_<CHANNEL>` (e.g. `TELEGRAM_CHAT_TRUMP`). Add the bot to each chat;
any channel without its own id falls back to `TELEGRAM_CHAT_ID`.

Tables: `feedback`, `muted_sources`, `muted_companies`, `training_examples`, plus
`alert_score` / `alert_suppressed_reason` on `detections`. Turn 🧪 examples into
tests (explicit, never automatic): `python scripts/generate_tests_from_training.py`.

## Sample alert

```
🚨 DELL — Dell Technologies Inc. (S&P 500)
HIGH · score 92 · CONFIRMED — primary source
💬 "go out and buy": Go out and buy a Dell, they're great.
🕒 2h ago · PRIMARY · rss:White House — News
🔗 https://example.com/original-source
Not financial advice.
```
Verdicts: CONFIRMED / CORROBORATED / REPORTED / UNVERIFIED.

## Run

```bash
python main.py            # continuous: polling + live feedback receiver
python main.py --once     # one cycle + drain feedback (used by schedulers)
pytest                    # tests (no network/keys/spaCy model needed)
```

**24/7 free:** GitHub Actions runs `--once` on a schedule — see
**[DEPLOY_GITHUB.md](DEPLOY_GITHUB.md)**. For an always-on host (instant feedback),
run `python main.py` on any small VM/Pi, or Docker: `docker compose up --build -d`.

> On the scheduled (`--once`) runner, a button tap is acknowledged on the *next*
> run (up to your interval later). For instant acknowledgement, run `main.py`
> continuously on an always-on host.

## Warnings & limits

- **Not financial advice; not auto-trading.** Verify every alert via its link.
- Verification reduces fakes but can't guarantee 100%.
- GitHub cron is best-effort (~10–20 min); not real-time.
- Detection isn't perfect — false positives/negatives happen; that's what the
  feedback loop is for.
