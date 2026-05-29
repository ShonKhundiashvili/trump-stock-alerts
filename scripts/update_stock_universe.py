"""Update the local stock universe CSV (data/stock_universe.csv).

The bot uses this local CSV at runtime so detection is fast and does NOT depend
on a network call for every post.

Strategy:
  1. Try to download an official, public listing (NASDAQ Trader symbol files) of
     US-listed equities. These are publicly published symbol directories.
  2. Normalize to: ticker, company_name, exchange, country, asset_type.
  3. If the download fails (offline / blocked / format change), keep the existing
     starter CSV and exit non-fatally with a clear message.

Usage:
    python scripts/update_stock_universe.py
    python scripts/update_stock_universe.py --output data/stock_universe.csv

Note: This script does NOT bypass any access controls. If the public endpoint
is unavailable, simply keep / hand-edit data/stock_universe.csv.
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
from pathlib import Path

import requests

logger = logging.getLogger("update_stock_universe")

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = BASE_DIR / "data" / "stock_universe.csv"

# NASDAQ Trader publishes pipe-delimited symbol directories publicly.
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

EXCHANGE_MAP = {
    "A": "NYSE American",
    "N": "NYSE",
    "P": "NYSE Arca",
    "Z": "Cboe BZX",
    "V": "IEX",
}


def _fetch(url: str, timeout: int = 30) -> str:
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "trump-stock-alerts/1.0"})
    resp.raise_for_status()
    return resp.text


def _parse_pipe(text: str):
    import csv

    rows = []
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    for row in reader:
        rows.append(row)
    # Last line is a "File Creation Time" footer; drop rows without a symbol.
    return rows


def build_rows():
    import pandas as pd

    records = []

    # NASDAQ-listed
    nasdaq = _parse_pipe(_fetch(NASDAQ_LISTED))
    for r in nasdaq:
        symbol = (r.get("Symbol") or "").strip()
        name = (r.get("Security Name") or "").strip()
        if not symbol or not name or r.get("Test Issue") == "Y":
            continue
        etf = r.get("ETF", "N")
        records.append({
            "ticker": symbol,
            "company_name": name,
            "exchange": "NASDAQ",
            "country": "US",
            "asset_type": "ETF" if etf == "Y" else "Equity",
        })

    # Other (NYSE etc.)
    other = _parse_pipe(_fetch(OTHER_LISTED))
    for r in other:
        symbol = (r.get("ACT Symbol") or r.get("NASDAQ Symbol") or "").strip()
        name = (r.get("Security Name") or "").strip()
        if not symbol or not name or r.get("Test Issue") == "Y":
            continue
        exch = EXCHANGE_MAP.get((r.get("Exchange") or "").strip(), "Other")
        etf = r.get("ETF", "N")
        records.append({
            "ticker": symbol,
            "company_name": name,
            "exchange": exch,
            "country": "US",
            "asset_type": "ETF" if etf == "Y" else "Equity",
        })

    df = pd.DataFrame.from_records(records).drop_duplicates(subset=["ticker"])
    df = df[df["ticker"].str.match(r"^[A-Z.\-]{1,6}$", na=False)]
    return df


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Update local stock universe CSV")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        df = build_rows()
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to update stock universe: %s", exc)
        logger.error(
            "Keeping existing %s. You can also edit it by hand "
            "(columns: ticker,company_name,exchange,country,asset_type).",
            output,
        )
        return 1

    if df.empty:
        logger.error("No rows parsed; keeping existing CSV.")
        return 1

    df.sort_values("ticker").to_csv(output, index=False)
    logger.info("Wrote %d tickers to %s", len(df), output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
