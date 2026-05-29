"""SEC stake-filing source — timely BIG-HOLDER ownership signals.

Uses SEC EDGAR full-text search to find recent Schedule 13D/13G filings (an
investor crossing ~5% ownership) BY major institutions (BlackRock, Vanguard,
State Street, Berkshire, …). These are filed within days (unlike quarterly 13F),
so they're real news: "BlackRock took/changed a >5% stake in X."

Each hit is relayed to the institutions channel; the subject company is resolved
to a ticker downstream. Official EDGAR API, no key, declared User-Agent.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import List

import requests

import db
from models import SourceItem, now_iso
from .base import BaseSource

logger = logging.getLogger(__name__)

EFTS = "https://efts.sec.gov/LATEST/search-index"
DEFAULT_FILERS = ["BlackRock", "Vanguard", "State Street", "Berkshire Hathaway",
                  "Capital Group", "Fidelity", "T. Rowe Price"]
_CIK_RE = re.compile(r"CIK\s*(\d+)", re.I)


def _name_and_cik(display: str):
    cik = None
    m = _CIK_RE.search(display)
    if m:
        cik = m.group(1)
    name = display.split("  (")[0].strip()
    return name, cik


class SECStakesSource(BaseSource):
    type = "sec_stakes"

    def __init__(self, conn: sqlite3.Connection, name: str = "stakes",
                 filers=None, lookback_days: int = 7, contact: str = "trump-stock-alerts dev@intername.media",
                 max_emit: int = 15, timeout: int = 20) -> None:
        super().__init__(conn, name=f"sec:{name}")
        self.filers = filers or DEFAULT_FILERS
        self.lookback_days = lookback_days
        self.contact = contact
        self.max_emit = max_emit
        self.timeout = timeout

    def _search(self, filer: str, start: str, end: str) -> List[SourceItem]:
        params = {"q": f'"{filer}"', "forms": "SC 13D,SC 13G",
                  "startdt": start, "enddt": end}
        try:
            resp = requests.get(EFTS, params=params,
                                headers={"User-Agent": self.contact}, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("[%s] EDGAR FTS error: %s", self.name, exc)
            return []
        if resp.status_code != 200:
            logger.warning("[%s] EDGAR FTS HTTP %s", self.name, resp.status_code)
            return []
        try:
            hits = resp.json().get("hits", {}).get("hits", [])
        except ValueError:
            return []

        out: List[SourceItem] = []
        for h in hits:
            src = h.get("_source", {})
            names = src.get("display_names", []) or []
            acc = (h.get("_id") or "").split(":")[0]
            if not names or not acc:
                continue
            # The display name NOT matching the queried filer is the subject company.
            issuer = next((n for n in names if filer.lower() not in n.lower()), names[0])
            issuer_name, issuer_cik = _name_and_cik(issuer)
            form = src.get("file_type") or "SC 13D/G"
            if db.source_item_exists(self.conn, self.name, acc):
                continue
            url = "https://www.sec.gov/cgi-bin/browse-edgar"
            if issuer_cik:
                acc_nodash = acc.replace("-", "")
                url = (f"https://www.sec.gov/Archives/edgar/data/{int(issuer_cik)}/"
                       f"{acc_nodash}/{acc}-index.htm")
            out.append(SourceItem(
                source=self.name,
                source_item_id=acc,
                url=url,
                text=f"{filer} filed {form} on {issuer_name} — ~5%+ stake.",
                timestamp=(src.get("file_date") or now_iso()),
                title=issuer_name,
            ))
        return out

    def fetch_new_items(self) -> List[SourceItem]:
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=self.lookback_days)
        items: List[SourceItem] = []
        for filer in self.filers:
            items.extend(self._search(filer, start.isoformat(), end.isoformat()))
            if len(items) >= self.max_emit:
                break
        self.touch()
        return items[: self.max_emit]
