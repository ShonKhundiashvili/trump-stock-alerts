"""YouTube source — catches spoken/video remarks (e.g. "go out and buy a Dell").

Compliance:
  - Discovery uses the OFFICIAL YouTube Data API v3 (needs YOUTUBE_API_KEY) to
    list a channel's recent uploads (title, description, publish time). No
    scraping, no login bypass.
  - OPTIONAL caption text: if `youtube-transcript-api` is installed and
    `fetch_transcripts` is enabled, the bot reads the video's PUBLIC captions so
    the full spoken text can be scanned. This uses YouTube's public caption
    endpoint; enable it at your discretion. If unavailable, we just use the
    title + description (still catches many reported quotes).

Recency: only videos published within the configured window are emitted (the
global recency gate also drops stale items downstream).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import List, Optional

import requests

import db
from models import SourceItem, now_iso
from .base import BaseSource

logger = logging.getLogger(__name__)

YT_API = "https://www.googleapis.com/youtube/v3"


def _fetch_transcript(video_id: str) -> str:
    """Best-effort public-caption fetch. Returns '' if unavailable/uninstalled."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # optional dep
    except Exception:
        return ""
    try:
        chunks = YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join(c.get("text", "") for c in chunks)[:20000]
    except Exception as exc:  # no captions / disabled / blocked — non-fatal
        logger.debug("No transcript for %s: %s", video_id, exc)
        return ""


class YouTubeSource(BaseSource):
    type = "youtube"

    def __init__(
        self,
        conn: sqlite3.Connection,
        name: str,
        api_key: Optional[str],
        channel_id: Optional[str] = None,
        handle: Optional[str] = None,
        fetch_transcripts: bool = True,
        max_videos: int = 10,
        timeout: int = 20,
    ) -> None:
        super().__init__(conn, name=f"youtube:{name}")
        self.display_name = name
        self.api_key = api_key
        self.channel_id = channel_id
        self.handle = handle
        self.fetch_transcripts = fetch_transcripts
        self.max_videos = max(1, min(max_videos, 50))
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> Optional[dict]:
        params = {**params, "key": self.api_key}
        try:
            resp = requests.get(f"{YT_API}/{path}", params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("[%s] YouTube API error: %s", self.name, exc)
            return None
        if resp.status_code != 200:
            logger.warning("[%s] YouTube API %s: %s", self.name, resp.status_code, resp.text[:200])
            return None
        return resp.json()

    def _uploads_playlist(self) -> Optional[str]:
        # Cache the channel's uploads-playlist id in source_state.extra.
        state = db.get_source_state(self.conn, self.name)
        if state and state["extra"]:
            try:
                cached = json.loads(state["extra"]).get("uploads")
                if cached:
                    return cached
            except json.JSONDecodeError:
                pass
        params = {"part": "contentDetails"}
        if self.channel_id:
            params["id"] = self.channel_id
        elif self.handle:
            params["forHandle"] = self.handle.lstrip("@")
        else:
            return None
        data = self._get("channels", params)
        items = (data or {}).get("items", [])
        if not items:
            return None
        uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        db.set_source_state(self.conn, self.name, extra=json.dumps({"uploads": uploads}))
        return uploads

    def fetch_new_items(self) -> List[SourceItem]:
        if not self.api_key:
            logger.warning("[%s] YOUTUBE_API_KEY not set; skipping", self.name)
            return []
        uploads = self._uploads_playlist()
        if not uploads:
            logger.warning("[%s] could not resolve channel uploads playlist", self.name)
            return []

        data = self._get("playlistItems", {
            "part": "snippet,contentDetails",
            "playlistId": uploads,
            "maxResults": self.max_videos,
        })
        items: List[SourceItem] = []
        for it in (data or {}).get("items", []):
            sn = it.get("snippet", {})
            vid = it.get("contentDetails", {}).get("videoId") or sn.get("resourceId", {}).get("videoId")
            if not vid or db.source_item_exists(self.conn, self.name, vid):
                continue
            published = it.get("contentDetails", {}).get("videoPublishedAt") or sn.get("publishedAt", "")
            title = sn.get("title", "")
            desc = sn.get("description", "")
            transcript = _fetch_transcript(vid) if self.fetch_transcripts else ""
            text = " ".join(t for t in (title, desc, transcript) if t).strip()
            items.append(SourceItem(
                source=self.name,
                source_item_id=vid,
                url=f"https://www.youtube.com/watch?v={vid}",
                text=text,
                timestamp=published or now_iso(),
                title=title,
            ))
        self.touch()
        return items
