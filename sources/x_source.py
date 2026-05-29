"""X / Twitter source adapter.

Uses the OFFICIAL X API v2 with a bearer token (X_BEARER_TOKEN). It does NOT
scrape X and does NOT bypass rate limits or Terms of Service.

Flow:
  - Resolve the configured username to a numeric user id (cached in source_state).
  - Fetch recent tweets with `since_id` set to the last seen tweet id.
  - Return them as SourceItems with the canonical tweet URL.

If no bearer token is configured, the adapter logs a warning and returns [].
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import List, Optional

import requests

import db
from models import SourceItem
from .base import BaseSource

logger = logging.getLogger(__name__)

X_API_BASE = "https://api.twitter.com/2"


class XSource(BaseSource):
    type = "x"

    def __init__(
        self,
        conn: sqlite3.Connection,
        account: str,
        bearer_token: Optional[str],
        max_results: int = 10,
        timeout: int = 20,
    ) -> None:
        super().__init__(conn, name=f"x:{account}")
        self.account = account.lstrip("@")
        self.bearer_token = bearer_token
        self.max_results = max(5, min(max_results, 100))
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.bearer_token}"}

    def _get_user_id(self) -> Optional[str]:
        # Cache the numeric user id in source_state.extra to avoid re-lookups.
        state = db.get_source_state(self.conn, self.name)
        if state and state["extra"]:
            try:
                cached = json.loads(state["extra"])
                if cached.get("user_id"):
                    return cached["user_id"]
            except json.JSONDecodeError:
                pass

        resp = requests.get(
            f"{X_API_BASE}/users/by/username/{self.account}",
            headers=self._headers(),
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            logger.error("X user lookup failed (%s): %s", resp.status_code, resp.text)
            return None
        user_id = resp.json().get("data", {}).get("id")
        if user_id:
            db.set_source_state(
                self.conn, self.name, extra=json.dumps({"user_id": user_id})
            )
        return user_id

    def fetch_new_items(self) -> List[SourceItem]:
        if not self.bearer_token:
            logger.warning("[%s] X_BEARER_TOKEN not set; skipping", self.name)
            return []

        user_id = self._get_user_id()
        if not user_id:
            return []

        params = {
            "max_results": self.max_results,
            "tweet.fields": "created_at,text",
            "exclude": "retweets,replies",
        }
        since_id = self.get_last_seen_id()
        if since_id:
            params["since_id"] = since_id

        resp = requests.get(
            f"{X_API_BASE}/users/{user_id}/tweets",
            headers=self._headers(),
            params=params,
            timeout=self.timeout,
        )
        if resp.status_code == 429:
            logger.warning("[%s] rate limited by X API; backing off this cycle", self.name)
            return []
        if resp.status_code != 200:
            logger.error("[%s] X tweets fetch failed (%s): %s",
                         self.name, resp.status_code, resp.text)
            return []

        payload = resp.json()
        tweets = payload.get("data", []) or []
        items: List[SourceItem] = []
        newest_id = since_id
        for tw in tweets:
            tid = tw["id"]
            items.append(
                SourceItem(
                    source=self.name,
                    source_item_id=tid,
                    url=f"https://x.com/{self.account}/status/{tid}",
                    text=tw.get("text", ""),
                    timestamp=tw.get("created_at", ""),
                )
            )
            if newest_id is None or int(tid) > int(newest_id):
                newest_id = tid

        if newest_id and newest_id != since_id:
            self.set_last_seen_id(newest_id)
        else:
            self.touch()
        return items
