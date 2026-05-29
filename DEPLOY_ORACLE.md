# Deploy trump-stock-alerts on Oracle Cloud (Always Free, $0/month)

This runs your bot 24/7 on a free Oracle Cloud VM. The bot makes only **outbound**
calls (fetch feeds → send Telegram), so you do **not** need a public IP, domain,
open ports, or a web server. You just need a small Linux box that stays on.

Time: ~20–30 min the first time (most of it is the Oracle signup).

---

## 0. What you'll end up with

An "Always Free" Ampere ARM VM running Ubuntu, with the bot in Docker,
auto-restarting on crash/reboot, polling every 10 minutes, messaging your
Telegram. Cost: $0 (Always Free resources are never billed).

---

## 1. Create an Oracle Cloud account

1. Go to **https://www.oracle.com/cloud/free/** → **Start for free**.
2. Sign up. A **credit card is required for identity verification**, but
   Always Free resources are **not charged**. Pick a **Home Region** close to you
   (you can't change it later).
3. Finish signup and log in to the Oracle Cloud Console.

> Tip: ARM ("Ampere A1") capacity is occasionally "out of capacity" in popular
> regions. If creation fails with that error, retry later or pick a different
> Availability Domain — it's a known Oracle quirk, not your mistake.

---

## 2. Create the Always Free VM

1. Console → **☰ Menu → Compute → Instances → Create instance**.
2. **Name:** `trump-stock-alerts`.
3. **Image and shape → Edit:**
   - **Image:** Canonical **Ubuntu 22.04**.
   - **Shape → Ampere → `VM.Standard.A1.Flex`**, set **1 OCPU / 6 GB RAM**
     (well within Always Free: up to 4 OCPU / 24 GB).
     *(If ARM is unavailable, the AMD `VM.Standard.E2.1.Micro` Always-Free shape
     also works — it's smaller but fine for this bot.)*
4. **Networking:** keep the default VCN/subnet, **Assign a public IPv4 address = Yes**
   (used only for you to SSH in).
5. **Add SSH keys:**
   - Easiest: choose **Generate a key pair for me** and **download the private key**.
   - Or paste your own public key (`~/.ssh/id_ed25519.pub`).
6. Click **Create**. Wait until the instance is **Running**, then copy its
   **Public IP address**.

> No ingress rules needed — the bot never accepts inbound connections. Default
> egress (outbound internet) is open, which is all it needs.

---

## 3. SSH into the VM (from your Mac)

```bash
# If Oracle generated the key, move it and lock permissions:
mv ~/Downloads/ssh-key-*.key ~/.ssh/oracle_trump.key
chmod 600 ~/.ssh/oracle_trump.key

# Connect (Ubuntu's default user is "ubuntu"):
ssh -i ~/.ssh/oracle_trump.key ubuntu@YOUR_PUBLIC_IP
```

If you used your own existing key, just:
```bash
ssh ubuntu@YOUR_PUBLIC_IP
```

---

## 4. Install Docker on the VM

Run these on the VM (the `ssh` session):

```bash
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y docker.io rsync curl
sudo systemctl enable --now docker

# Install the Docker Compose v2 plugin (works on ARM aarch64 and x86_64):
ARCH=$(uname -m)
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${ARCH}" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

sudo usermod -aG docker $USER
# Apply the group change:
exit
```
Then SSH back in:
```bash
ssh -i ~/.ssh/oracle_trump.key ubuntu@YOUR_PUBLIC_IP
docker version   # should print client+server with no 'permission denied'
```

---

## 5. Copy the project from your Mac to the VM

Run this **on your Mac** (note the trailing slashes). It uploads the whole
project, including your `.env`:

```bash
rsync -avz -e "ssh -i ~/.ssh/oracle_trump.key" \
  --exclude '.venv' --exclude '__pycache__' --exclude '*.db' --exclude '.pytest_cache' \
  "/Users/shonsmac/Desktop/stock bot/trump-stock-alerts/" \
  ubuntu@YOUR_PUBLIC_IP:~/trump-stock-alerts/
```

> We exclude `.venv` and `*.db` on purpose: the VM builds its own environment in
> Docker, and the database is created fresh on the server (so you don't carry
> over the already-seen items from your Mac — a clean start on the server is what
> you want).

Your secrets live in `.env`, which got copied. Double-check on the VM:
```bash
cat ~/trump-stock-alerts/.env   # confirm tokens + POLL_SECONDS=600
```

---

## 6. Build and start the bot

On the VM:
```bash
cd ~/trump-stock-alerts
docker compose up --build -d
```
The first build takes a few minutes (it installs deps + the spaCy model). Then:

```bash
docker compose logs -f
```
You should see `Starting trump-stock-alerts`, the source list, and poll cycles.
Press `Ctrl+C` to stop *watching* logs (the bot keeps running).

`docker-compose.yml` already sets `restart: unless-stopped`, so the bot
auto-restarts on crash **and** on VM reboot.

---

## 7. Verify it's alive

```bash
docker compose ps                  # State should be "running"
docker compose logs --tail 50      # recent activity
```
Within ~10 minutes (your poll interval) it will have done a cycle. When Trump
talks up a company on a monitored source, you'll get the Telegram alert.

---

## Day-to-day operations

**View logs:**
```bash
cd ~/trump-stock-alerts && docker compose logs -f
```

**Change settings / sources** (edit on the VM, then restart):
```bash
nano ~/trump-stock-alerts/.env                 # e.g. MIN_ALERT_CONFIDENCE, POLL_SECONDS
nano ~/trump-stock-alerts/config/sources.json  # add/remove feeds
docker compose restart
```

**Push updated code from your Mac later:** re-run the `rsync` command from step 5,
then `docker compose up --build -d` on the VM.

**Stop / start:**
```bash
docker compose down     # stop
docker compose up -d    # start
```

**Update the stock universe occasionally:**
```bash
docker compose exec trump-stock-alerts python scripts/update_stock_universe.py
```

**Database & data** persist in a Docker volume + the mounted `data/` dir, so they
survive restarts and rebuilds.

---

## Alternative: run without Docker (systemd)

If you'd rather not use Docker:

```bash
cd ~/trump-stock-alerts
sudo apt-get install -y python3.11 python3.11-venv
python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

Create `/etc/systemd/system/trump-stock-alerts.service`:
```ini
[Unit]
Description=trump-stock-alerts
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/trump-stock-alerts
ExecStart=/home/ubuntu/trump-stock-alerts/.venv/bin/python main.py
EnvironmentFile=/home/ubuntu/trump-stock-alerts/.env
Restart=always
RestartSec=10
User=ubuntu

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now trump-stock-alerts
journalctl -u trump-stock-alerts -f
```

---

## Security notes

- Your `.env` holds live tokens. Keep the VM's SSH key private; don't share the
  `.env`. You can rotate the Telegram token anytime via @BotFather → `/revoke`.
- Only SSH (port 22) is reachable. Consider restricting it to your IP in the
  VCN's Security List if you want to harden further.
- Oracle Always Free won't bill you, but **idle Always-Free ARM instances can be
  reclaimed** if truly idle for long periods; a continuously-running bot like
  this keeps it active.
