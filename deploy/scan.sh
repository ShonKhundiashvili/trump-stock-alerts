#!/bin/bash
# Daily equity-scan launcher for trump-stock-alerts.
# Runs ONE pass of the S&P 500 + Nasdaq-100 technical/fundamental screener
# (main.py --scan) and posts the ranked report to the Daily Scan topic, then
# exits. Scheduled by launchd (com.trumpstockalerts.scan) on weekdays.
# Research only, not financial advice.
set -euo pipefail

APP_DIR="/Users/shonsmac/Apps/trump-stock-alerts"
cd "$APP_DIR"

# Use the project venv's interpreter directly (robust even if the venv was moved;
# `activate` can hardcode an old path). Falls back to system python3.
if [ -x ".venv/bin/python" ]; then
  exec ".venv/bin/python" main.py --scan
else
  exec python3 main.py --scan
fi
