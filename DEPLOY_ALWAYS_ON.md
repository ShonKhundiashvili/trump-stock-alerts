# Always-on hosting — instant alerts

GitHub Actions cron is free but **flaky and slow** (it batches and often skips
the `*/10` schedule, so an alert can land 10–40 min late). For speed, run the
bot as a **continuous process** that polls every 30–60 s. Two good options:

| Option | Cost | Best when |
|--------|------|-----------|
| **A. Your Mac (launchd)** | free | the Mac is on most of the day; simplest |
| **B. Cheap Linux VPS (systemd)** | ~$5/mo | you want true 24/7, even when the Mac sleeps |

Both run `python main.py` (the continuous loop) instead of `--once`. Set
`POLL_SECONDS=45` in `.env` for near-instant alerts.

> Reminder: this is research tooling. No auto-trading, no advice. Keep all
> secrets in `.env` (already git-ignored) — never commit them.

---

## Option A — Mac, always-on via launchd

This keeps the bot running in the background and **relaunches it automatically**
if it crashes or after a reboot/login.

1. Make the launcher executable:
   ```sh
   chmod +x "/Users/shonsmac/Desktop/stock bot/trump-stock-alerts/deploy/run.sh"
   ```

2. Set a fast cadence in `.env`:
   ```sh
   POLL_SECONDS=45
   ```

3. Install the LaunchAgent (per-user; no sudo needed):
   ```sh
   cp "/Users/shonsmac/Desktop/stock bot/trump-stock-alerts/deploy/com.trumpstockalerts.bot.plist" \
      ~/Library/LaunchAgents/

   launchctl unload ~/Library/LaunchAgents/com.trumpstockalerts.bot.plist 2>/dev/null
   launchctl load   ~/Library/LaunchAgents/com.trumpstockalerts.bot.plist
   ```

4. Confirm it's running and watch the log:
   ```sh
   launchctl list | grep trumpstockalerts        # shows a PID when alive
   tail -f "/Users/shonsmac/Desktop/stock bot/trump-stock-alerts/data/bot.out.log"
   ```

   You should see “Starting trump-stock-alerts [loop every 45s]” then cycle lines.

**Stop / restart:**
```sh
launchctl unload ~/Library/LaunchAgents/com.trumpstockalerts.bot.plist   # stop
launchctl load   ~/Library/LaunchAgents/com.trumpstockalerts.bot.plist   # start
```

**Keep it running while the lid is closed (optional):** so polling continues
when the Mac would normally sleep, either keep it plugged in with
`System Settings → Battery → prevent sleep when display off`, or run:
```sh
# while plugged in, never sleep (revert with: sudo pmset -c sleep 1)
sudo pmset -c sleep 0
```
If the Mac sleeps, polling simply pauses and resumes on wake — no data loss
(the SQLite cursor persists), you just won't get alerts during sleep. For true
24/7 use Option B.

---

## Option B — $5 Linux VPS, true 24/7 via systemd

1. Create a small VPS (Hetzner CX22 / DigitalOcean / Lightsail / Oracle Free
   Tier). Ubuntu 22.04+ is fine.

2. On the VPS:
   ```sh
   sudo adduser --disabled-password botuser
   sudo su - botuser
   git clone <your-repo-url> trump-stock-alerts   # or scp the folder up
   cd trump-stock-alerts
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   python -m spacy download en_core_web_sm        # NER model
   cp .env.example .env && nano .env              # paste your tokens; POLL_SECONDS=45
   exit
   ```

3. Install + start the service (as root):
   ```sh
   sudo cp /home/botuser/trump-stock-alerts/deploy/trump-stock-alerts.service \
           /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now trump-stock-alerts
   ```

4. Verify and tail logs:
   ```sh
   systemctl status trump-stock-alerts
   journalctl -u trump-stock-alerts -f
   ```

**Update later:**
```sh
sudo su - botuser -c 'cd trump-stock-alerts && git pull'
sudo systemctl restart trump-stock-alerts
```

---

## Daily equity scan + performance scorecard

The continuous loop handles real-time alerts. The **daily scan** and the
**performance outcome refresh** are separate jobs:

- On a VPS, add cron entries (run weekdays after the US close):
  ```cron
  35 21 * * 1-5  cd /home/botuser/trump-stock-alerts && .venv/bin/python main.py --scan
  ```
  `--scan` runs the equity scanner **and** refreshes signal-performance outcomes
  (so `/performance` stays current). To refresh outcomes only: `main.py --perf`.

- On the Mac, you can keep using the existing GitHub Actions `scan.yml`, or add a
  second LaunchAgent with a `StartCalendarInterval` if you prefer it local.

You can always trigger these from Telegram: `/scan` (run the scan now) and
`/performance` (hit-rate + average move, with `1d` / `3d` / `7d`).
