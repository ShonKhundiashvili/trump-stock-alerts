"""Generate pytest cases from your 🧪 'Mark as Training Example' feedback.

This runs ONLY when you invoke it explicitly. It never runs automatically and it
never changes detector code or config — it just writes/overwrites a test file
(tests/test_training_examples.py) that locks in expected behaviour for the
examples you curated from Telegram.

How expectations are derived (deterministic, transparent):
  - If the same detection ever got a ❌ "fake" label   -> expect the ticker is
    NOT detected (a negative/regression test).
  - Otherwise (useful / not_useful / needs_context / no other label) the mention
    is real, so we expect the ticker IS detected.
  - Generation uses the detector with spaCy disabled (so the test runs in CI
    without the model). If an example's ticker is only detectable via spaCy NER,
    the case is emitted as `skip` with a reason rather than a misleading assert.

Usage:
    python scripts/generate_tests_from_training.py
    python scripts/generate_tests_from_training.py --db alerts.db --output tests/test_training_examples.py
    python scripts/generate_tests_from_training.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import config_loader  # noqa: E402
import db  # noqa: E402
from detector import Detector  # noqa: E402
from ticker_resolver import TickerResolver  # noqa: E402

DEFAULT_OUTPUT = BASE_DIR / "tests" / "test_training_examples.py"

HEADER = '''"""AUTO-GENERATED from training_examples — DO NOT EDIT BY HAND.

Regenerate with:
    python scripts/generate_tests_from_training.py

Each case comes from a 🧪 "Mark as Training Example" you saved in Telegram.
"""

import json
from pathlib import Path

import pytest

from detector import Detector
from ticker_resolver import TickerResolver

BASE_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def detector():
    with (BASE_DIR / "config" / "watchlist.json").open() as fh:
        watchlist = json.load(fh)
    with (BASE_DIR / "config" / "phrases.json").open() as fh:
        phrases = json.load(fh)
    with (BASE_DIR / "config" / "priority_tickers.json").open() as fh:
        priority = json.load(fh)
    resolver = TickerResolver(
        watchlist=watchlist,
        universe_path=BASE_DIR / "data" / "stock_universe.csv",
        enable_online=False,
        index_tickers=set(priority.get("ALL", [])),
    )
    return Detector(resolver=resolver, phrases=phrases, watchlist=watchlist,
                    priority_tickers=priority, use_spacy=False)


CASES = json.loads(r"""
%s
""")


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_training_example(detector, case):
    if case["expect"] == "skip":
        pytest.skip(case["reason"])
    tickers = {r.ticker for r in detector.detect(case["text"])}
    if case["expect"] == "detect":
        assert case["ticker"] in tickers, (
            f"expected {case['ticker']} to be detected in: {case['text']!r}"
        )
    else:  # reject
        assert case["ticker"] not in tickers, (
            f"expected {case['ticker']} NOT to be detected in: {case['text']!r}"
        )
'''


def build_detector():
    watchlist = config_loader.load_watchlist()
    phrases = config_loader.load_phrases()
    priority = config_loader.load_priority_tickers()
    resolver = TickerResolver(watchlist=watchlist, enable_online=False,
                              index_tickers=set(priority.get("ALL", [])))
    return Detector(resolver=resolver, phrases=phrases, watchlist=watchlist,
                    priority_tickers=priority, use_spacy=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate pytest cases from training examples")
    parser.add_argument("--db", default=os.getenv("DATABASE_PATH", "alerts.db"))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--dry-run", action="store_true", help="Print, don't write")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"No database at {db_path}; nothing to generate.")
        return 1

    conn = db.connect(str(db_path))
    db.init_db(conn)
    rows = conn.execute(
        "SELECT * FROM training_examples WHERE text IS NOT NULL ORDER BY id"
    ).fetchall()
    if not rows:
        print("No training examples found. Mark some alerts with 🧪 in Telegram first.")
        # Still write an empty (skipped) file so the suite stays green.
        cases = [{"id": "no_examples", "expect": "skip", "ticker": "",
                  "text": "", "reason": "No training examples saved yet."}]
        _write(args, cases)
        return 0

    detector = build_detector()
    cases = []
    seen_ids = set()
    for r in rows:
        text = r["text"] or ""
        ticker = (r["ticker"] or "").upper()
        det_id = r["detection_id"]

        # Negative if this detection was ever labelled 'fake'.
        labels = {
            f["feedback_label"]
            for f in db.feedback_for_detection(conn, det_id)
        } if det_id else set()
        is_fake = "fake" in labels

        detected = ticker in {d.ticker for d in detector.detect(text)} if ticker else False

        if is_fake:
            expect, reason = "reject", ""
        elif not ticker:
            expect, reason = "skip", "training example has no ticker"
        elif detected:
            expect, reason = "detect", ""
        else:
            # Real example, but not detectable without spaCy NER in CI.
            expect, reason = "skip", f"{ticker} only detectable via spaCy NER; not asserted in CI"

        cid = f"ex{r['id']}_{ticker or 'none'}_{expect}"
        if cid in seen_ids:
            cid = f"{cid}_{len(cases)}"
        seen_ids.add(cid)
        cases.append({"id": cid, "expect": expect, "ticker": ticker,
                      "text": text, "reason": reason})

    _write(args, cases)
    n_detect = sum(1 for c in cases if c["expect"] == "detect")
    n_reject = sum(1 for c in cases if c["expect"] == "reject")
    n_skip = sum(1 for c in cases if c["expect"] == "skip")
    print(f"{len(cases)} case(s): {n_detect} detect, {n_reject} reject, {n_skip} skip")
    return 0


def _write(args, cases) -> None:
    payload = json.dumps(cases, indent=2)
    content = HEADER % payload
    if args.dry_run:
        print(content)
        return
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    sys.exit(main())
