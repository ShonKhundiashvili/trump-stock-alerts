# Deploy trump-stock-alerts on GitHub Actions (free, no credit card, no server)

This runs your bot **in GitHub's cloud on a schedule** (~every 15 min). No server,
no credit card, $0. Your tokens live in GitHub's encrypted **Secrets** — never in
the code. The workflow runs `python main.py --once` (one poll cycle) each time and
persists its state (dedup + "last seen") between runs via the Actions cache.

> The repo must be **public** to get unlimited free Actions minutes. Public means
> your *code* is visible — that's fine (it's not sensitive). Your **tokens are NOT
> in the code**; they're stored as encrypted secrets and never exposed.

---

## 1. Create a free GitHub account

Go to **https://github.com/signup** and create an account (no card needed).

## 2. Create a new PUBLIC repository

1. https://github.com/new
2. **Repository name:** `trump-stock-alerts`
3. Select **Public**.
4. Do **not** add a README/.gitignore/license (you already have them).
5. **Create repository.** Leave that page open — it shows push commands.

## 3. Push the project to the repo

Your `.env` and `*.db` are git-ignored, so **secrets are NOT uploaded** — good.

### Option A — command line (on your Mac)
```bash
cd "/Users/shonsmac/Desktop/stock bot/trump-stock-alerts"
git init
git add .
git commit -m "Initial commit: trump-stock-alerts"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/trump-stock-alerts.git
git push -u origin main
```
When pushing over HTTPS, GitHub asks for your username and a **Personal Access
Token** (not your password). Create one at
**https://github.com/settings/tokens** → *Generate new token (classic)* → check
**repo** scope → use it as the password. (Or install the `gh` CLI and run
`gh auth login` first, which handles auth for you.)

### Option B — GitHub Desktop (easiest, GUI)
1. Install **https://desktop.github.com**.
2. **File → Add Local Repository** → choose the `trump-stock-alerts` folder →
   *create a repository* when prompted.
3. **Publish repository** → make sure **"Keep this code private" is UNCHECKED**
   (it must be public) → Publish.

After pushing, confirm on GitHub that you can see `main.py`, `config/`, and
`.github/workflows/poll.yml` — but **NOT** `.env` (it must be absent).

## 4. Add your tokens as repository Secrets

On the repo page: **Settings → Secrets and variables → Actions → New repository secret.**
Add these (name must match exactly):

| Secret name | Value |
|-------------|-------|
| `TELEGRAM_BOT_TOKEN` | your bot token |
| `TELEGRAM_CHAT_ID`   | `6044588942` (your chat id) |

Optional (only if you want them later): `X_BEARER_TOKEN`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`. The bot works fine without them.

## 5. Enable Actions & run it once to test

1. Go to the **Actions** tab. If prompted, click **"I understand my workflows,
   go ahead and enable them."**
2. In the left list, click **poll-trump-stock-alerts**.
3. Click **Run workflow → Run workflow** (the `workflow_dispatch` button) to do an
   immediate test run instead of waiting for the schedule.
4. Click the run to watch the logs. The **Run one poll cycle** step should show it
   building sources and finishing with "Single cycle complete." If there's a
   MEDIUM/HIGH hit, you'll get a Telegram message.

After that, it runs automatically on the schedule.

---

## How it behaves

- **Schedule:** `*/15 * * * *` in `.github/workflows/poll.yml`. GitHub cron is
  **best-effort** and frequently delayed under load — expect ~10–20 min between
  runs, sometimes more. That's normal for free Actions and fine for this use case.
- **Change the frequency:** edit the `cron:` line and push. (Going below ~10 min
  rarely helps because of GitHub's throttling.)
- **State:** dedup + "last seen id" are cached as `alerts.db` between runs, so you
  won't get repeat alerts for the same item.
- **Cost:** $0. Public repos get unlimited Actions minutes.

## Updating the bot later

Edit files locally, then:
```bash
git add . && git commit -m "update" && git push
```
(or use GitHub Desktop). Config lives in `config/*.json` — change sources,
watchlist, or phrases and push; the next run picks them up.

## Manual run anytime

**Actions tab → poll-trump-stock-alerts → Run workflow.**

---

## Known limitations (read this)

- **60-day auto-pause:** GitHub disables *scheduled* workflows if the repository
  has **no commit activity for 60 days**. The cache-based state doesn't create
  commits, so to keep it alive indefinitely either (a) push any small change every
  ~6 weeks, or (b) if alerts ever stop, go to the **Actions** tab and click the
  **Enable workflow** button — it resumes immediately.
- **Not real-time:** minimum effective cadence is ~10–15 min, and Telegram alerts
  depend on the source feeds updating. This is a monitor, not a millisecond signal.
- **Public code:** anyone can read your detection logic/config. That's fine — no
  secrets are in the repo. Never commit `.env`.
