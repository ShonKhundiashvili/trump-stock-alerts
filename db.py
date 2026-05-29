"""SQLite storage and deduplication layer.

Tables:
  - source_items : every raw item we have fetched (unique per source+id)
  - detections   : classified company/ticker mentions
  - alert_log    : record of alerts actually sent (dedupe key)
  - source_state : per-source cursor (e.g. last seen tweet id)

Deduplication:
  - A source item is only stored once (UNIQUE(source, source_item_id)).
  - An alert is only sent once per (source, source_item_id, ticker)
    (UNIQUE(source, source_item_id, ticker) in alert_log).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from models import DetectionResult, SourceItem, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    source_item_id  TEXT NOT NULL,
    url             TEXT,
    text            TEXT,
    timestamp       TEXT,
    priority        TEXT,
    canonical_url   TEXT,
    text_hash       TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE(source, source_item_id)
);

CREATE TABLE IF NOT EXISTS detections (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_item_rowid             INTEGER,
    source                        TEXT NOT NULL,
    source_item_id                TEXT NOT NULL,
    url                           TEXT,
    text                          TEXT,
    timestamp                     TEXT,
    company                       TEXT,
    ticker                        TEXT,
    candidate_tickers             TEXT,
    confidence                    TEXT,
    text_confidence               TEXT,
    source_priority               TEXT,
    verification_status           TEXT,
    ticker_resolution_confidence  REAL,
    matched_phrase                TEXT,
    alert_sent                    INTEGER NOT NULL DEFAULT 0,
    created_at                    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alert_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    source_item_id  TEXT NOT NULL,
    ticker          TEXT,
    text_hash       TEXT,
    detection_id    INTEGER,
    sent_at         TEXT NOT NULL,
    UNIQUE(source, source_item_id, ticker)
);

CREATE TABLE IF NOT EXISTS source_state (
    source        TEXT PRIMARY KEY,
    last_seen_id  TEXT,
    last_polled   TEXT,
    extra         TEXT
);
"""


def connect(database_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first schema, for pre-existing DBs."""
    wanted = {
        "source_items": {"priority": "TEXT", "canonical_url": "TEXT", "text_hash": "TEXT"},
        "detections": {
            "text_confidence": "TEXT",
            "source_priority": "TEXT",
            "verification_status": "TEXT",
        },
        "alert_log": {"text_hash": "TEXT"},
    }
    for table, cols in wanted.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col, coltype in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
    conn.commit()


# --------------------------------------------------------------------------- #
# source_items
# --------------------------------------------------------------------------- #
def source_item_exists(conn: sqlite3.Connection, source: str, source_item_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM source_items WHERE source = ? AND source_item_id = ?",
        (source, source_item_id),
    ).fetchone()
    return row is not None


def insert_source_item(conn: sqlite3.Connection, item: SourceItem) -> Optional[int]:
    """Insert a source item. Returns the new rowid, or None if it already existed."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO source_items
            (source, source_item_id, url, text, timestamp,
             priority, canonical_url, text_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (item.source, item.source_item_id, item.url, item.text, item.timestamp,
         item.priority, item.canonical_url, item.text_hash, now_iso()),
    )
    conn.commit()
    if cur.rowcount == 0:
        return None  # duplicate, ignored
    return cur.lastrowid


# --------------------------------------------------------------------------- #
# detections
# --------------------------------------------------------------------------- #
def insert_detection(
    conn: sqlite3.Connection,
    item: SourceItem,
    detection: DetectionResult,
    source_item_rowid: Optional[int],
) -> int:
    text_conf = (detection.text_confidence or detection.confidence)
    cur = conn.execute(
        """
        INSERT INTO detections
            (source_item_rowid, source, source_item_id, url, text, timestamp,
             company, ticker, candidate_tickers, confidence, text_confidence,
             source_priority, verification_status,
             ticker_resolution_confidence, matched_phrase, alert_sent, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            source_item_rowid,
            item.source,
            item.source_item_id,
            item.url,
            detection.text_excerpt,
            item.timestamp,
            detection.company_name,
            detection.ticker,
            json.dumps(detection.candidate_tickers),
            detection.confidence.value,
            text_conf.value if text_conf else None,
            detection.source_priority,
            detection.verification_status,
            detection.ticker_resolution_confidence,
            detection.matched_phrase,
            now_iso(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def mark_alert_sent(conn: sqlite3.Connection, detection_id: int) -> None:
    conn.execute("UPDATE detections SET alert_sent = 1 WHERE id = ?", (detection_id,))
    conn.commit()


def update_detection_verdict(
    conn: sqlite3.Connection,
    detection_id: int,
    confidence: str,
    verification_status: str,
) -> None:
    conn.execute(
        "UPDATE detections SET confidence = ?, verification_status = ? WHERE id = ?",
        (confidence, verification_status, detection_id),
    )
    conn.commit()


def corroboration(
    conn: sqlite3.Connection, ticker: str, window_hours: int = 48
) -> tuple[bool, int]:
    """Return (primary_source_found, distinct_secondary_source_count) for a ticker
    across all sources within the recent window. Used for cross-source verification.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    primary = conn.execute(
        """
        SELECT 1 FROM detections
        WHERE ticker = ? AND source_priority = 'PRIMARY' AND created_at >= ?
        LIMIT 1
        """,
        (ticker, cutoff),
    ).fetchone()
    sec = conn.execute(
        """
        SELECT COUNT(DISTINCT source) FROM detections
        WHERE ticker = ? AND source_priority = 'SECONDARY' AND created_at >= ?
        """,
        (ticker, cutoff),
    ).fetchone()
    return (primary is not None, int(sec[0]) if sec else 0)


def alert_sent_for_text_hash(
    conn: sqlite3.Connection, text_hash: Optional[str], ticker: Optional[str]
) -> bool:
    """Cross-source dedup: has an alert already gone out for this exact text + ticker?

    Catches the same statement reposted/reported across multiple sources.
    """
    if not text_hash:
        return False
    row = conn.execute(
        "SELECT 1 FROM alert_log WHERE text_hash = ? AND IFNULL(ticker,'') = IFNULL(?, '') LIMIT 1",
        (text_hash, ticker),
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------- #
# alert_log (dedupe)
# --------------------------------------------------------------------------- #
def alert_already_sent(
    conn: sqlite3.Connection, source: str, source_item_id: str, ticker: Optional[str]
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM alert_log
        WHERE source = ? AND source_item_id = ? AND IFNULL(ticker, '') = IFNULL(?, '')
        """,
        (source, source_item_id, ticker),
    ).fetchone()
    return row is not None


def record_alert(
    conn: sqlite3.Connection,
    source: str,
    source_item_id: str,
    ticker: Optional[str],
    detection_id: Optional[int] = None,
    text_hash: Optional[str] = None,
) -> bool:
    """Record an alert. Returns True if newly recorded, False if it was a duplicate."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO alert_log
            (source, source_item_id, ticker, text_hash, detection_id, sent_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (source, source_item_id, ticker, text_hash, detection_id, now_iso()),
    )
    conn.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# source_state
# --------------------------------------------------------------------------- #
def get_source_state(conn: sqlite3.Connection, source: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM source_state WHERE source = ?", (source,)
    ).fetchone()


def get_last_seen_id(conn: sqlite3.Connection, source: str) -> Optional[str]:
    row = get_source_state(conn, source)
    return row["last_seen_id"] if row else None


def set_source_state(
    conn: sqlite3.Connection,
    source: str,
    last_seen_id: Optional[str] = None,
    extra: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO source_state (source, last_seen_id, last_polled, extra)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            last_seen_id = COALESCE(excluded.last_seen_id, source_state.last_seen_id),
            last_polled  = excluded.last_polled,
            extra        = COALESCE(excluded.extra, source_state.extra)
        """,
        (source, last_seen_id, now_iso(), extra),
    )
    conn.commit()


def recent_detections(conn: sqlite3.Connection, limit: int = 20) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM detections ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
