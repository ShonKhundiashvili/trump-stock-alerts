"""Generic public webpage / transcript adapter.

Fetches a public URL, extracts readable text, and emits a SourceItem when the
content changes. It:
  - Respects a minimum polling interval (avoid hammering the server).
  - Honors timeouts and handles errors gracefully.
  - Uses an optional CSS selector to target the relevant content.
  - Does NOT bypass logins, paywalls, Cloudflare, or rate limits.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

import requests

import db
from models import SourceItem, now_iso
from .base import BaseSource

logger = logging.getLogger(__name__)

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None  # type: ignore

USER_AGENT = "trump-stock-alerts/1.0 (+respectful public monitoring; contact via .env owner)"


class WebpageSource(BaseSource):
    type = "webpage"

    def __init__(
        self,
        conn: sqlite3.Connection,
        name: str,
        url: str,
        selector: Optional[str] = None,
        min_interval_seconds: int = 1800,
        timeout: int = 20,
    ) -> None:
        super().__init__(conn, name=f"web:{name}")
        self.display_name = name
        self.url = url
        self.selector = selector
        self.min_interval_seconds = min_interval_seconds
        self.timeout = timeout

    def _too_soon(self) -> bool:
        state = db.get_source_state(self.conn, self.name)
        if not state or not state["last_polled"]:
            return False
        try:
            last = datetime.fromisoformat(state["last_polled"])
        except ValueError:
            return False
        delta = (datetime.now(timezone.utc) - last).total_seconds()
        return delta < self.min_interval_seconds

    def _extract_text(self, html: str) -> str:
        if BeautifulSoup is None:
            return html
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
            tag.decompose()
        if self.selector:
            nodes = soup.select(self.selector)
            if nodes:
                return "\n".join(n.get_text(" ", strip=True) for n in nodes)
        return soup.get_text(" ", strip=True)

    def fetch_new_items(self) -> List[SourceItem]:
        if self._too_soon():
            logger.debug("[%s] polled recently; skipping", self.name)
            return []

        try:
            resp = requests.get(
                self.url,
                headers={"User-Agent": USER_AGENT},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.warning("[%s] request error: %s", self.name, exc)
            self.touch()
            return []

        if resp.status_code != 200:
            logger.warning("[%s] HTTP %s; skipping", self.name, resp.status_code)
            self.touch()
            return []

        text = self._extract_text(resp.text)
        content_hash = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:24]

        # Skip if the content hasn't changed since last time.
        last_hash = self.get_last_seen_id()
        if content_hash == last_hash:
            self.touch()
            return []

        self.set_last_seen_id(content_hash)
        item = SourceItem(
            source=self.name,
            source_item_id=content_hash,
            url=self.url,
            text=text[:20000],  # cap stored text
            timestamp=now_iso(),
            title=self.display_name,
        )
        return [item]
