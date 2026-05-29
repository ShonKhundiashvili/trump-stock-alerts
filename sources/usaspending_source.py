"""USAspending federal-contracts source — beyond-Trump catalyst signals.

Recent large federal CONTRACT awards (e.g. a defense contract to Dell) are
market-moving and fully public. This adapter queries the OFFICIAL, free
USAspending.gov API for recent awards above a $ threshold and emits each as a
PRIMARY item; the detector resolves the recipient name to a ticker and only
public companies surface. Official API, no key, no scraping.

The emitted text contains catalyst language ("awarded a ... government contract
worth $X") so it scores as a bullish MEDIUM signal through the normal pipeline.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import List

import requests

import db
from models import SourceItem, now_iso
from .base import BaseSource

logger = logging.getLogger(__name__)

API = "https://api.usaspending.gov/api/v2/search/spending_by_award/"


def _fmt_amount(amount) -> str:
    try:
        a = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    if a >= 1e9:
        return f"${a/1e9:.1f}B"
    if a >= 1e6:
        return f"${a/1e6:.0f}M"
    return f"${a:,.0f}"


class USASpendingSource(BaseSource):
    type = "usaspending"

    def __init__(
        self,
        conn: sqlite3.Connection,
        name: str = "federal contracts",
        min_amount: float = 50_000_000,
        lookback_days: int = 7,
        max_records: int = 25,
        timeout: int = 25,
    ) -> None:
        super().__init__(conn, name=f"usaspending:{name}")
        self.min_amount = min_amount
        self.lookback_days = lookback_days
        self.max_records = max(1, min(max_records, 100))
        self.timeout = timeout

    def fetch_new_items(self) -> List[SourceItem]:
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=self.lookback_days)
        body = {
            "filters": {
                "award_type_codes": ["A", "B", "C", "D"],  # contracts
                "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat()}],
                "award_amounts": [{"lower_bound": self.min_amount}],
            },
            "fields": ["Award ID", "Recipient Name", "Award Amount",
                       "Awarding Agency", "Start Date", "Description"],
            "sort": "Award Amount",
            "order": "desc",
            "limit": self.max_records,
            "page": 1,
        }
        try:
            resp = requests.post(API, json=body, timeout=self.timeout,
                                 headers={"User-Agent": "trump-stock-alerts/1.0"})
        except requests.RequestException as exc:
            logger.warning("[%s] USAspending error: %s", self.name, exc)
            self.touch()
            return []
        if resp.status_code != 200:
            logger.warning("[%s] USAspending HTTP %s", self.name, resp.status_code)
            self.touch()
            return []

        items: List[SourceItem] = []
        for r in resp.json().get("results", []) or []:
            award_id = str(r.get("Award ID") or r.get("generated_internal_id") or "")
            recipient = (r.get("Recipient Name") or "").strip()
            if not award_id or not recipient:
                continue
            if db.source_item_exists(self.conn, self.name, award_id):
                continue
            agency = (r.get("Awarding Agency") or "").strip()
            amount = _fmt_amount(r.get("Award Amount"))
            desc = (r.get("Description") or "").strip()
            text = f"{recipient} awarded a {agency} government contract worth {amount}. {desc}".strip()
            items.append(SourceItem(
                source=self.name,
                source_item_id=award_id,
                url="https://www.usaspending.gov/award/" + award_id,
                text=text,
                timestamp=r.get("Start Date") or now_iso(),
                title=f"{recipient}: {amount} {agency} contract",
            ))
        self.touch()
        return items
