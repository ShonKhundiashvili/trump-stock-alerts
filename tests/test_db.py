"""Tests for the SQLite storage / dedupe layer."""

import pytest

import db
from models import Confidence, DetectionResult, SourceItem


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


def _item(item_id="123"):
    return SourceItem(
        source="x:realDonaldTrump",
        source_item_id=item_id,
        url="https://x.com/realDonaldTrump/status/123",
        text="Go out and buy a Dell.",
        timestamp="2026-05-08T14:31:00Z",
    )


def _detection(ticker="DELL"):
    return DetectionResult(
        company_name="Dell Technologies Inc.",
        ticker=ticker,
        candidate_tickers=[ticker],
        confidence=Confidence.HIGH,
        ticker_resolution_confidence=98.0,
        matched_phrase="go out and buy",
        text_excerpt="Go out and buy a Dell.",
    )


def test_insert_source_item(conn):
    rowid = db.insert_source_item(conn, _item())
    assert rowid is not None
    assert db.source_item_exists(conn, "x:realDonaldTrump", "123")


def test_no_duplicate_source_item(conn):
    first = db.insert_source_item(conn, _item())
    second = db.insert_source_item(conn, _item())
    assert first is not None
    assert second is None  # duplicate ignored


def test_no_duplicate_alert_same_item_and_ticker(conn):
    item = _item()
    rowid = db.insert_source_item(conn, item)
    det_id = db.insert_detection(conn, item, _detection(), rowid)

    assert db.alert_already_sent(conn, item.source, item.source_item_id, "DELL") is False
    first = db.record_alert(conn, item.source, item.source_item_id, "DELL", det_id)
    assert first is True
    assert db.alert_already_sent(conn, item.source, item.source_item_id, "DELL") is True

    # Second attempt for same item + ticker is suppressed.
    second = db.record_alert(conn, item.source, item.source_item_id, "DELL", det_id)
    assert second is False


def test_different_ticker_same_item_allowed(conn):
    item = _item()
    db.insert_source_item(conn, item)
    assert db.record_alert(conn, item.source, item.source_item_id, "DELL") is True
    # A different ticker on the same item is a separate alert.
    assert db.record_alert(conn, item.source, item.source_item_id, "INTC") is True


def test_source_state_roundtrip(conn):
    db.set_source_state(conn, "x:realDonaldTrump", last_seen_id="999")
    assert db.get_last_seen_id(conn, "x:realDonaldTrump") == "999"
    # Updating without last_seen_id preserves it.
    db.set_source_state(conn, "x:realDonaldTrump")
    assert db.get_last_seen_id(conn, "x:realDonaldTrump") == "999"


def test_mark_alert_sent(conn):
    item = _item()
    rowid = db.insert_source_item(conn, item)
    det_id = db.insert_detection(conn, item, _detection(), rowid)
    db.mark_alert_sent(conn, det_id)
    row = conn.execute("SELECT alert_sent FROM detections WHERE id = ?", (det_id,)).fetchone()
    assert row["alert_sent"] == 1


def _item_for(source, item_id, priority):
    it = SourceItem(source=source, source_item_id=item_id,
                    url=f"https://x/{item_id}", text="buy DELL",
                    timestamp="2026-05-29T10:00:00Z", priority=priority)
    return it


def _det_p(priority):
    d = _detection()
    d.source_priority = priority
    return d


def test_corroboration_counts_primary_and_distinct_secondary(conn):
    # 1 primary + 2 distinct secondary sources flag DELL.
    for src, prio, iid in [
        ("rss:Truth Social", "PRIMARY", "p1"),
        ("rss:CNBC", "SECONDARY", "s1"),
        ("news_search:Trump buy", "SECONDARY", "s2"),
        ("rss:CNBC", "SECONDARY", "s3"),  # same source again -> still 1 distinct
    ]:
        it = _item_for(src, iid, prio)
        rid = db.insert_source_item(conn, it)
        db.insert_detection(conn, it, _det_p(prio), rid)

    primary_found, secondary_count = db.corroboration(conn, "DELL", window_hours=48)
    assert primary_found is True
    assert secondary_count == 2  # CNBC + news_search (distinct), not 3


def test_corroboration_none_for_unknown_ticker(conn):
    primary_found, secondary_count = db.corroboration(conn, "ZZZZ", window_hours=48)
    assert primary_found is False
    assert secondary_count == 0


def test_cross_source_text_hash_dedup(conn):
    # Same statement reposted by two different sources -> second is a dupe.
    db.record_alert(conn, "rss:Truth Social", "p1", "DELL", text_hash="abc123")
    assert db.alert_sent_for_text_hash(conn, "abc123", "DELL") is True
    # Different source, same text+ticker -> already covered.
    assert db.alert_sent_for_text_hash(conn, "abc123", "DELL") is True
    # Different ticker -> not covered.
    assert db.alert_sent_for_text_hash(conn, "abc123", "INTC") is False
