"""SEC stake-filing source — timely institutional ownership signals.

Pulls the SEC EDGAR "latest filings" feed for Schedule 13D / 13G (and amendments)
— the filings made when an investor crosses ~5% ownership of a company. These are
filed within days (unlike quarterly 13F), so they're actual news ("a major holder
took a >5% stake in X"). Official EDGAR feed, no key, declared User-Agent.

Each filing is relayed to the institutions channel; the subject company in the
filing title is resolved to a ticker downstream.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import List

import requests

import db
from models import SourceItem, now_iso
from .base import BaseSource

logger = logging.getLogger(__name__)

EDGAR = "https://www.sec.gov/cgi-bin/browse-edgar"
# Title looks like: "SC 13D/A - GENCO SHIPPING & TRADING LTD (0001326200) (Subject)"
_TITLE_RE = re.compile(r"^(SC 13[DG](?:/A)?)\s*-\s*(.+?)\s*\(\d+\)\s*\(Subject\)", re.I)

FORM_TYPES = ["SC 13D", "SC 13G"]


class SECStakesSource(BaseSource):
    type = "sec_stakes"

    def __init__(self, conn: sqlite3.Connection, name: str = "stakes",
                 contact: str = "trump-stock-alerts dev@intername.media",
                 max_emit: int = 12, count: int = 60, timeout: int = 20) -> None:
        super().__init__(conn, name=f"sec:{name}")
        self.contact = contact
        self.max_emit = max_emit
        self.count = count
        self.timeout = timeout

    def _fetch_form(self, form: str) -> List[SourceItem]:
        try:
            import feedparser
        except Exception:
            return []
        params = {"action": "getcurrent", "type": form, "company": "",
                  "owner": "include", "count": self.count, "output": "atom"}
        try:
            resp = requests.get(EDGAR, params=params,
                                headers={"User-Agent": self.contact}, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("[%s] EDGAR error: %s", self.name, exc)
            return []
        if resp.status_code != 200:
            logger.warning("[%s] EDGAR HTTP %s", self.name, resp.status_code)
            return []

        out: List[SourceItem] = []
        for e in feedparser.parse(resp.text).entries:
            title = getattr(e, "title", "") or ""
            m = _TITLE_RE.match(title)
            if not m:
                continue
            form_type, company = m.group(1).upper(), m.group(2).strip()
            link = getattr(e, "link", "")
            item_id = (getattr(e, "id", None) or link or title)[-48:]
            if db.source_item_exists(self.conn, self.name, item_id):
                continue
            out.append(SourceItem(
                source=self.name,
                source_item_id=item_id,
                url=link or "https://www.sec.gov/cgi-bin/browse-edgar",
                text=f"{company}: {form_type} filed — investor crossed ~5% stake.",
                timestamp=getattr(e, "updated", "") or now_iso(),
                title=company,
            ))
        return out

    def fetch_new_items(self) -> List[SourceItem]:
        items: List[SourceItem] = []
        for form in FORM_TYPES:
            items.extend(self._fetch_form(form))
            if len(items) >= self.max_emit:
                break
        self.touch()
        return items[: self.max_emit]
