# trump-stock-alerts

A Telegram alert bot that monitors **public** Trump-related sources for mentions
of public companies, tickers, stocks, or buy/investment-style language, then
sends you a **concise, action-focused** Telegram notification with the
**original source link** and a **confidence level**.

It does **not** rely only on a static watchlist — it dynamically recognizes
public companies and tickers (via cashtags, ticker tokens, spaCy NER, and fuzzy
matching against a local stock universe), even for companies you never manually
added.

You can **classify each alert right from Telegram** with inline buttons, and the
bot **learns from your feedback** to improve future scoring and filtering — all
local, rule-based, and transparent (no model training, no auto-trading, no
advice). It also filters for **recency** (stale posts are suppressed) and does
**cross-source verification** to cut down on fakes. See
[Human-in-the-loop feedback](#human-in-the-loop-feedback) and
[Recency &amp; verification](#recency--verification).

---

## What it does

- Polls configured sources (X/Twitter official API, RSS/transcripts, public
  webpages, Truth Social placeholder).
- Normalizes text, detects companies/tickers/cashtags.
- Resolves detected companies to stock tickers dynamically.
- Detects buy/investment/stock-call language.
- Assigns confidence: **HIGH / MEDIUM / LOW** (and **NONE** = no alert).
- Filters for **recency** — only recent, still-actionable posts alert; old/stale
  posts are stored but suppressed (configurable `max_age_hours` in
  `config/alerting.json`).
- Performs **cross-source verification** — a lone, unverified "Breaking: Trump
  said buy X" from a single source is flagged **UNVERIFIED** / suppressed unless
  a primary source or multiple independent sources corroborate it. This reduces
  false alerts but cannot guarantee 100% — always verify via the source link.
- Stores every item and detection in SQLite.
- Sends a **concise, action-focused** Telegram alert (deduplicated per
  source-item + ticker) with inline buttons to classify it.
- **Learns from your Telegram feedback** to tune future scoring/filtering —
  local, rule-based, transparent (see
  [Human-in-the-loop feedback](#human-in-the-loop-feedback)).
- Optionally adds an LLM "second opinion" (classification only) if a key is set.

## What it does NOT do

- ❌ No auto-trading. It never places orders.
- ❌ No financial advice. It classifies text only; it never tells you to buy or sell.
- ❌ No bypassing logins, paywalls, Cloudflare, rate limits, or Terms of Service.
- ❌ No scraping of X — it uses the **official X API** only.
- The Truth Social adapter is a **compliant placeholder** (see below).

Every alert includes the original source link so **you verify manually**.

---

## Project layout

```
trump-stock-alerts/
  main.py                 # continuous polling loop
  detector.py             # detection pipeline (cashtags, tokens, NER, phrases)
  ticker_resolver.py      # dynamic company -> ticker resolution
  llm_classifier.py       # OPTIONAL LLM second opinion (classification only)
  telegram_alerts.py      # Telegram Bot API alerter
  db.py                   # SQLite storage + dedupe
  models.py               # typed data models
  config_loader.py        # env + JSON config loading
  requirements.txt
  README.md
  .env.example
  Dockerfile
  docker-compose.yml
  config/
    sources.json          # which sources are enabled
    watchlist.json         # priority aliases / overrides
    phrases.json           # HIGH / MEDIUM phrase lists
  data/
    stock_universe.csv    # local ticker universe (fast, offline detection)
  scripts/
    update_stock_universe.py
  sources/
    base.py x_source.py rss_source.py webpage_source.py truthsocial_source.py
  tests/
    test_detector.py test_ticker_resolver.py test_db.py
```

---

## Setup

Requires **Python 3.11+**.

```bash
cd trump-stock-alerts
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm     # enables dynamic NER detection

cp .env.example .env                          # then edit .env (see below)
```

> The bot still runs if the spaCy model isn't installed — it just falls back to
> watchlist + cashtag + ticker-token detection (no open-ended NER).

### Create a Telegram bot (BotFather)

1. In Telegram, open **@BotFather**.
2. Send `/newbot`, choose a name and a username ending in `bot`.
3. BotFather returns a **bot token** — put it in `.env` as `TELEGRAM_BOT_TOKEN`.

### Get your Telegram chat ID

1. Send any message to your new bot (so it can message you back).
2. Open in a browser (replace `<TOKEN>`):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Find `"chat":{"id":...}` in the JSON. That number is your `TELEGRAM_CHAT_ID`.
   (For a group chat the id is negative — that's expected.)

### Add your X API token

1. Get a bearer token from the X Developer Portal (official API).
2. Put it in `.env` as `X_BEARER_TOKEN`.
3. If you leave it blank, the X source is simply skipped.

### (Optional) LLM key

Set `OPENAI_API_KEY` **or** `ANTHROPIC_API_KEY` to enable the optional
second-opinion classifier. Leave both blank to use rule-based detection only.
Install the matching library: `pip install openai` or `pip install anthropic`.

---

## Configuration

### `config/sources.json`

Enable/disable sources and list what to monitor:

```json
{
  "x":   { "enabled": true,  "accounts": ["realDonaldTrump"], "max_results": 10 },
  "rss": { "enabled": true,  "feeds": [{ "name": "White House", "url": "https://.../feed/" }] },
  "webpages": { "enabled": false, "pages": [
      { "name": "Transcript", "url": "https://example.com/x", "selector": "article", "min_interval_seconds": 1800 }
  ]},
  "truthsocial": { "enabled": false, "accounts": ["realDonaldTrump"], "rss_url": "" }
}
```

- **x** — accounts to monitor via the official X API. Default: `realDonaldTrump`.
- **rss** — RSS/Atom feeds (great for **speeches/transcripts** — see note below).
- **webpages** — public pages; optional CSS `selector`; `min_interval_seconds`
  enforces respectful polling.
- **truthsocial** — disabled placeholder; only set `rss_url` if you have a
  compliant public feed (see `sources/truthsocial_source.py`).

### `config/watchlist.json`

Used **only** for priority aliases / manual overrides / disambiguation —
detection is not limited to it. Ships with Dell, Intel, Apple, Nvidia, Tesla,
Boeing, Microsoft, Amazon, Alphabet, Meta, AMD, Palantir.

```json
{ "Dell Technologies": { "ticker": "DELL", "company_name": "Dell Technologies Inc.", "aliases": ["Dell", "Dell Technologies"] } }
```

### `config/phrases.json`

`HIGH` = explicit buy/invest language; `MEDIUM` = positive business language.
A company/ticker mention with neither is classified `LOW`.

### Controlling alert volume (`MIN_ALERT_CONFIDENCE`)

Every detection is stored in SQLite, but only detections at/above
`MIN_ALERT_CONFIDENCE` (in `.env`) are pushed to Telegram:

- `HIGH` — only explicit buy/investment calls ("go out and buy a Dell").
- `MEDIUM` (default) — also positive business language ("Tesla is a great company").
- `LOW` — also any bare company/ticker mention. **Very noisy** — a single White
  House fact sheet can name many companies. Not recommended for Telegram.

Use `LOW` only if you want to capture everything; otherwise keep `MEDIUM`.

### Source priority & cross-source verification (`config/source_priority.json`)

Sources are grouped into provenance tiers, and the **final alert confidence**
combines the text classification with where the information came from:

| Tier | Examples | Max confidence on its own |
|------|----------|---------------------------|
| **PRIMARY** | Trump's posts (Truth Social archive / X), White House, Roll Call transcripts | **HIGH** (direct statement) |
| **SECONDARY** | CNBC, MarketWatch, Yahoo, Benzinga, WSJ, Google-News keyword searches | **MEDIUM** from one source; **HIGH** only if corroborated (a PRIMARY source or ≥2 independent SECONDARY sources also have it) |
| **SOCIAL_RUMOR** | Reddit search feeds, reposts | **LOW**, always labelled *"Unverified social signal — check primary source before acting."* |

Every Telegram alert shows **Source priority**, **Verification** status, and
**Primary source found: Yes/No**. Edit `config/source_priority.json` to retier a
source (longest matching `overrides` prefix wins, then per-type `defaults`).

Non-PRIMARY sources are filtered to items that actually mention Trump
(`require_keywords`), so general market news doesn't trigger on every company.

**`config/sources.json`** now has these groups (all editable):
- `rss` — PRIMARY transcript feeds + SECONDARY news feeds (each with a `priority`).
- `news_search` — Google-News RSS keyword templates (`"Trump says buy"`, etc.).
- `reddit` — SOCIAL_RUMOR early-warning feeds (disabled by default — noisy).
- `webpages`, `x`, `truthsocial` — as before.

Cross-source **deduplication**: the same statement reported by multiple outlets
alerts only once (matched by canonical URL + normalized text hash).

### Recency & verification

**Recency.** Only posts newer than `max_age_hours` (default **48**) can alert.
Older items are still polled and stored, but suppressed with the reason
`stale` — so a long-buried post never fires a "fresh" alert. Set `max_age_hours`
in `config/alerting.json`.

**Verification verdict.** Each alert carries one of:

- **CONFIRMED** — a primary source carries the claim directly.
- **CORROBORATED** — multiple independent sources carry it.
- **REPORTED** — a single secondary source has it (not yet corroborated).
- **UNVERIFIED** — an uncorroborated social / single-source strong claim
  ("Breaking: Trump said buy X").

Uncorroborated strong social claims are **penalized or suppressed**, so a single
unverified rumor doesn't trigger a high-confidence alert. This cuts false alerts
but **cannot guarantee 100%** — always verify via the source link. Two
`config/alerting.json` flags control this:

- `social_requires_corroboration` — require a primary/independent corroborating
  source before a social/single-source strong claim can alert.
- `penalize_uncorroborated` — dock the alert score of uncorroborated claims
  rather than (or in addition to) suppressing them.

These work alongside the score gating in
[Human-in-the-loop feedback](#human-in-the-loop-feedback) (`min_alert_score`,
`send_low_confidence`, `send_social_rumor`, `social_rumor_min_score`).

### Update `data/stock_universe.csv`

The bot resolves tickers from this local CSV (fast, no per-post network calls).
A starter CSV of major US stocks ships with the repo. To refresh it from the
public NASDAQ symbol directories:

```bash
python scripts/update_stock_universe.py
```

If the download is unavailable, the existing CSV is kept. You can also hand-edit
it — columns: `ticker,company_name,exchange,country,asset_type`.

---

## Human-in-the-loop feedback

Every alert carries inline Telegram buttons so **you** classify it, and the bot
learns your preferences over time — locally, rule-based, and transparent. It
never trades, never gives advice, and never edits code or config on its own.

**Buttons under each alert:**

| Button | Meaning |
|--------|---------|
| ✅ Useful / Real Signal | Good alert — raises the score for this source/company/phrase |
| ❌ Fake / Wrong | Wrong detection — strongly lowers the source score |
| ⚠️ Real but Not Useful | Correct but not actionable — slightly lowers the score |
| 🧵 Needs More Context | Ambiguous — slight negative unless a primary source confirms |
| 🚫 Mute This Source | Stop alerting from this source (still stored, marked `muted_source`) |
| 🔕 Mute This Company | Stop alerting for this ticker (still stored, marked `muted_company`) |
| 📈 Too Late | Flags a latency problem; does **not** count against correctness |
| 🧪 Mark as Training Example | Saves the full example to `training_examples` for later review |

When you tap a button, Telegram sends a callback; the bot stores your label in
SQLite, replies "Feedback saved: …", and never crashes on old/invalid taps.
Only your configured `TELEGRAM_CHAT_ID` is honoured — other chats are ignored.

**How learning affects future alerts.** Each alert gets an `alert_score` (0–100):

```
base: HIGH 85 | MEDIUM 60 | LOW 30
+10 primary source   |  -15 social rumor   |  +15 clear direct buy phrase
-20 ambiguous ticker
+ user source quality (-20..+20)  + company relevance (-15..+15)
+ phrase quality (-10..+10)        # all derived from your feedback history
```

An alert is sent only if it passes the mutes **and** the score thresholds in
`config/alerting.json` (`min_alert_score`, `send_low_confidence`,
`send_social_rumor`, `social_rumor_min_score`). Suppressed alerts are still
stored with a reason, so nothing is hidden. The scoring is deterministic — the
same inputs always yield the same score.

**Telegram commands** (from your chat only):

- `/stats` — totals, useful/fake/not-useful counts, top/worst sources, top tickers, mutes
- `/mutes` — list muted sources and companies
- `/unmute_source <source>` — remove a source from the mute list
- `/unmute_company <ticker>` — remove a ticker from the mute list
- `/recent` — last 5 alerts and their feedback status
- `/help` — command list

**Where it's stored:** all local in SQLite — tables `feedback`, `muted_sources`,
`muted_companies`, `training_examples`, plus `alert_score` /
`alert_suppressed_reason` on `detections`. Nothing is auto-deleted.

**Turning training examples into tests (explicit, never automatic).** When you
tap 🧪 on alerts, run this when *you* choose to lock that behaviour into the
test suite:

```bash
python scripts/generate_tests_from_training.py
pytest tests/test_training_examples.py
```

It writes `tests/test_training_examples.py` from your saved examples: a detection
ever labelled ❌ becomes a *negative* test (ticker must NOT be detected); others
become *positive* tests (ticker must be detected). It only writes the test file —
it never edits detector code or config.

**Architecture:** the feedback receiver long-polls `getUpdates` in a background
thread alongside source polling. On the scheduled (`--once`) runner it instead
*drains* pending taps at the start of each run (the update offset is persisted in
the DB), so feedback made between runs is picked up on the next run. Enable/disable
with `ENABLE_TELEGRAM_FEEDBACK` in `.env`.

---

## Run locally

```bash
source .venv/bin/activate
python main.py
```

Poll interval comes from `POLL_SECONDS` (default 60). Stop with `Ctrl+C`.
If Telegram isn't configured, alerts are **logged** instead of sent (useful for
a dry run).

## Run tests

```bash
pip install pytest
pytest                 # all tests
pytest tests/test_detector.py -v
```

Tests don't require network access, the spaCy model, or any API keys.

## Run 24/7 in the cloud (free)

The bot only makes outbound calls (feeds → Telegram), so it doesn't need a public
IP, domain, or open ports — just something that runs it on a schedule:

- **[DEPLOY_GITHUB.md](DEPLOY_GITHUB.md)** — **GitHub Actions** (recommended free
  option): runs every ~15 min in GitHub's cloud. **$0, no credit card, no server.**
  Tokens stored as encrypted Actions secrets. Uses `python main.py --once`.
- **[DEPLOY_ORACLE.md](DEPLOY_ORACLE.md)** — Oracle Cloud "Always Free" VM (also
  $0, but signup needs a non-prepaid card and can be flaky).

`python main.py --once` runs a single poll cycle and exits (for schedulers/cron);
`python main.py` runs the continuous loop.

## Run with Docker

```bash
cp .env.example .env    # fill in tokens
docker compose up --build -d
docker compose logs -f
docker compose down
```

`docker-compose.yml` loads `.env`, mounts `config/` and `data/`, persists the
SQLite DB in a named volume, and restarts unless stopped.

## Run with systemd on a VPS

Create `/etc/systemd/system/trump-stock-alerts.service`:

```ini
[Unit]
Description=trump-stock-alerts
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/trump-stock-alerts
ExecStart=/opt/trump-stock-alerts/.venv/bin/python main.py
EnvironmentFile=/opt/trump-stock-alerts/.env
Restart=always
RestartSec=10
User=botuser

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now trump-stock-alerts
sudo journalctl -u trump-stock-alerts -f
```

---

## Sample Telegram alert

Alerts are concise and action-focused, and carry inline buttons so you can
classify them (see [Human-in-the-loop feedback](#human-in-the-loop-feedback)):

```
🚨 DELL — Dell Technologies Inc. (S&P 500)
HIGH · score 92 · CONFIRMED — primary source
💬 "go out and buy": Go out and buy a Dell, they're great.
🕒 2h ago · PRIMARY · rss:White House — News
🔗 https://example.com/original-source
Not financial advice.

[✅ Useful] [❌ Fake]  [⚠️ Not Useful] [🧵 Needs Context]
[🚫 Mute Source] [🔕 Mute Company]  [📈 Too Late] [🧪 Training]
```

The verdict line shows confidence, the 0–100 alert score, and a verification
status (CONFIRMED / CORROBORATED / REPORTED / UNVERIFIED) from cross-source
checking; `🕒 2h ago` is the post's age (stale posts are not alerted).

---

## Why speeches / transcripts matter

Some market-moving comments are made in **speeches, press briefings, or
interviews** and are never posted to social media. The RSS/transcript and
webpage adapters exist so those public statements are monitored too — not just
social posts.

---

## ⚠️ Warnings

- **This is not financial advice.** The system only classifies text.
- **Always verify the source manually** using the link in each alert. Detection
  (especially fuzzy/NER matching and any LLM step) can be wrong.
- It does not trade and never will on your behalf.

## Limitations / TODOs

- Truth Social: placeholder only. Wire in an **official API or compliant public
  RSS** if/when available (see `sources/truthsocial_source.py`). No scraping.
- Ticker resolution prefers US-listed equities and the watchlist; ambiguous
  matches are flagged and never alerted as HIGH.
- The starter `stock_universe.csv` is a small sample — run
  `scripts/update_stock_universe.py` for full coverage.
- spaCy NER quality depends on the installed model; consider a larger model for
  better organization recognition.
- X API access depends on your developer tier and rate limits.
