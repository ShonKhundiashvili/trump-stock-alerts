"""Truth Social placeholder adapter.

IMPORTANT / COMPLIANCE
----------------------
This is a PLACEHOLDER that implements the standard source interface but does
NOT scrape Truth Social, and does NOT bypass logins, Cloudflare, rate limits,
or Terms of Service.

TODO (compliant integration only):
  - If/when an OFFICIAL Truth Social API or a publicly provided RSS/Atom feed
    becomes available, wire it in here using a bearer token / documented
    endpoint, the same way `x_source.py` uses the official X API.
  - Truth Social is built on Mastodon-compatible software; if a public,
    ToS-permitted Mastodon API endpoint is exposed for an account, you may use
    the documented `/api/v1/accounts/:id/statuses` endpoint with appropriate
    auth and rate limiting. Only do this if it is explicitly permitted.
  - Do NOT add headless-browser scraping, Cloudflare bypass, or login automation.

Until a compliant access path is configured, this adapter:
  - Optionally consumes a user-provided public RSS mirror URL (rss_url in config),
    treating it exactly like the RSSSource (respectful polling, no bypass).
  - Otherwise returns no items (and logs that it is idle).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import List, Optional

from models import SourceItem
from .base import BaseSource
from .rss_source import RSSSource

logger = logging.getLogger(__name__)


class TruthSocialSource(BaseSource):
    type = "truthsocial"

    def __init__(
        self,
        conn: sqlite3.Connection,
        account: str,
        rss_url: Optional[str] = None,
    ) -> None:
        super().__init__(conn, name=f"truthsocial:{account}")
        self.account = account.lstrip("@")
        self.rss_url = rss_url
        # If the user supplies a compliant public RSS mirror, delegate to RSSSource.
        self._delegate: Optional[RSSSource] = None
        if rss_url:
            self._delegate = RSSSource(conn=conn, name=f"truthsocial-{self.account}", url=rss_url)

    def fetch_new_items(self) -> List[SourceItem]:
        if self._delegate is not None:
            # Re-tag delegate items under this source name for clarity.
            items = self._delegate.fetch_new_items()
            for it in items:
                it.source = self.name
            return items

        # No compliant access configured. Stay idle rather than scrape.
        logger.info(
            "[%s] idle: no official API / public RSS configured. "
            "See truthsocial_source.py TODO for compliant integration.",
            self.name,
        )
        self.touch()
        return []
