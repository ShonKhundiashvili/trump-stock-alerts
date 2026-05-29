"""Signal performance tracking.

For every alert we actually sent (a detection with a resolved ticker), record the
entry price at alert time and the forward return at +1 / +3 / +7 trading days
(via yfinance). `summary()` turns that into a scorecard — hit-rate and average
move, overall and by source — surfaced through the Telegram /performance command.

This measures whether the *signals* tended to move in the called direction. It is
a backward-looking research scorecard, NOT a track record, a guarantee, or advice.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from models import now_iso

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 35          # how far back to (re)compute outcomes
_HORIZONS = (("ret_1d", 1), ("ret_3d", 3), ("ret_7d", 7))


def _alert_rows_needing_outcomes(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    """Sent alerts (with a ticker) in the lookback window whose 7d return isn't filled yet."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    return conn.execute(
        """
        SELECT d.id, d.ticker, d.source, d.matched_phrase, d.direction, d.created_at
        FROM detections d
        LEFT JOIN signal_performance p ON p.detection_id = d.id
        WHERE d.alert_sent = 1
          AND d.ticker IS NOT NULL AND d.ticker != ''
          AND d.created_at >= ?
          AND (p.detection_id IS NULL OR p.ret_7d IS NULL)
        ORDER BY d.created_at
        """,
        (cutoff,),
    ).fetchall()


def _upsert(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT INTO signal_performance
            (detection_id, ticker, source, matched_phrase, direction,
             alert_date, entry_price, ret_1d, ret_3d, ret_7d, updated_at)
        VALUES (:detection_id, :ticker, :source, :matched_phrase, :direction,
                :alert_date, :entry_price, :ret_1d, :ret_3d, :ret_7d, :updated_at)
        ON CONFLICT(detection_id) DO UPDATE SET
            entry_price = excluded.entry_price,
            ret_1d = excluded.ret_1d,
            ret_3d = excluded.ret_3d,
            ret_7d = excluded.ret_7d,
            updated_at = excluded.updated_at
        """,
        row,
    )


def update_outcomes(conn: sqlite3.Connection) -> int:
    """Backfill forward returns for sent alerts. Returns rows updated. Best-effort."""
    pending = _alert_rows_needing_outcomes(conn)
    if not pending:
        return 0
    # One price fetch per distinct ticker covers all its alerts.
    by_ticker: Dict[str, List[sqlite3.Row]] = {}
    for r in pending:
        by_ticker.setdefault(r["ticker"].upper(), []).append(r)

    try:
        import yfinance as yf
    except Exception as exc:  # noqa: BLE001
        logger.warning("performance.update_outcomes: yfinance unavailable: %s", exc)
        return 0

    updated = 0
    for ticker, rows in by_ticker.items():
        sym = ticker.replace(".", "-")
        try:
            df = yf.Ticker(sym).history(period="3mo")
        except Exception as exc:  # noqa: BLE001
            logger.debug("perf history(%s) failed: %s", sym, exc)
            continue
        if df is None or df.empty:
            continue
        closes = df["Close"]
        # Index dates as naive date objects for comparison.
        dates = [d.date() for d in closes.index.to_pydatetime()]
        for r in rows:
            try:
                alert_date = datetime.fromisoformat(
                    r["created_at"].replace("Z", "+00:00")
                ).date()
            except (ValueError, TypeError):
                continue
            # First trading session on/after the alert date = entry.
            entry_idx = next((i for i, d in enumerate(dates) if d >= alert_date), None)
            if entry_idx is None:
                continue
            entry = float(closes.iloc[entry_idx])
            if entry <= 0:
                continue
            rec = {
                "detection_id": r["id"], "ticker": ticker, "source": r["source"],
                "matched_phrase": r["matched_phrase"], "direction": r["direction"] or "bullish",
                "alert_date": alert_date.isoformat(), "entry_price": round(entry, 4),
                "ret_1d": None, "ret_3d": None, "ret_7d": None, "updated_at": now_iso(),
            }
            for col, n in _HORIZONS:
                j = entry_idx + n
                if j < len(closes):
                    rec[col] = round(float(closes.iloc[j]) / entry - 1, 4)
            _upsert(conn, rec)
            updated += 1
    conn.commit()
    logger.info("performance: updated %d alert outcome(s) across %d ticker(s)",
                updated, len(by_ticker))
    return updated


def _directional(ret: Optional[float], direction: str) -> Optional[bool]:
    """Did the move go the called way? None if no return yet."""
    if ret is None:
        return None
    if direction == "bearish":
        return ret < 0
    return ret > 0   # bullish / neutral treated as "expected up"


def summary(conn: sqlite3.Connection, horizon: str = "ret_3d") -> str:
    """Human-readable scorecard for the /performance command."""
    if horizon not in {h for h, _ in _HORIZONS}:
        horizon = "ret_3d"
    rows = conn.execute(
        f"SELECT ticker, source, direction, entry_price, {horizon} AS ret "
        "FROM signal_performance WHERE entry_price IS NOT NULL"
    ).fetchall()
    scored = [r for r in rows if r["ret"] is not None]
    label = {"ret_1d": "+1d", "ret_3d": "+3d", "ret_7d": "+7d"}[horizon]
    if not scored:
        n_pending = len(rows)
        return (f"📈 Signal performance ({label})\n"
                f"No matured outcomes yet"
                + (f" ({n_pending} alert(s) still aging)." if n_pending else ".")
                + "\nOutcomes fill in 1–7 trading days after each alert.")

    hits = [_directional(r["ret"], r["direction"]) for r in scored]
    n_hit = sum(1 for h in hits if h)
    n = len(scored)
    avg = sum(r["ret"] for r in scored) / n
    wins = [r["ret"] for r, h in zip(scored, hits) if h]
    losses = [r["ret"] for r, h in zip(scored, hits) if not h]
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0

    # Per-source hit-rate (min 2 samples).
    by_src: Dict[str, List[bool]] = {}
    for r, h in zip(scored, hits):
        by_src.setdefault(r["source"].split(":")[0], []).append(bool(h))
    src_lines = []
    for src, hs in sorted(by_src.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(hs) >= 2:
            src_lines.append(f"  {src}: {sum(hs)}/{len(hs)} ({sum(hs)/len(hs)*100:.0f}%)")

    best = max(scored, key=lambda r: r["ret"])
    worst = min(scored, key=lambda r: r["ret"])

    out = [
        f"📈 Signal performance ({label}) — research scorecard",
        f"Samples: {n} · Hit-rate: {n_hit}/{n} ({n_hit/n*100:.0f}%)",
        f"Avg move: {avg*100:+.1f}% · avg win {avg_win*100:+.1f}% · avg loss {avg_loss*100:+.1f}%",
        f"Best: {best['ticker']} {best['ret']*100:+.1f}% · Worst: {worst['ticker']} {worst['ret']*100:+.1f}%",
    ]
    if src_lines:
        out.append("By source (hit-rate):")
        out.extend(src_lines)
    out.append("Backward-looking signal study — not a track record or advice.")
    return "\n".join(out)
