#!/bin/bash
# Always-on launcher for trump-stock-alerts.
# Runs the continuous polling loop (main.py with no args). POLL_SECONDS in .env
# controls cadence — set it to 30–60 on a dedicated host for near-instant alerts.
set -euo pipefail

APP_DIR="/Users/shonsmac/Desktop/stock bot/trump-stock-alerts"
cd "$APP_DIR"

# Activate the project venv if present, else fall back to system python3.
if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

exec python main.py
