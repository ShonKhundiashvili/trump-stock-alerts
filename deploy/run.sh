#!/bin/bash
# Always-on launcher for trump-stock-alerts.
# Runs the continuous polling loop (main.py with no args). POLL_SECONDS in .env
# controls cadence — set it to 30–60 on a dedicated host for near-instant alerts.
set -euo pipefail

APP_DIR="/Users/shonsmac/Apps/trump-stock-alerts"
cd "$APP_DIR"

# Use the project venv's interpreter directly (robust even if the venv was moved;
# `activate` can hardcode an old path). Falls back to system python3.
if [ -x ".venv/bin/python" ]; then
  exec ".venv/bin/python" main.py
else
  exec python3 main.py
fi
