"""Tests for the source-priority + verification policy."""

import alert_policy
from models import Confidence, DetectionResult


def _det(text_conf=Confidence.HIGH):
    return DetectionResult(
        company_name="Dell Technologies Inc.",
        ticker="DELL",
        candidate_tickers=["DELL"],
        confidence=text_conf,            # detector puts text confidence here
        ticker_resolution_confidence=98.0,
        matched_phrase="go out and buy",
        text_excerpt="Go out and buy a Dell.",
    )


PRIORITY_CFG = {
    "DEFAULT": "PRIMARY",
    "defaults": {"rss": "PRIMARY", "news_search": "SECONDARY", "reddit": "SOCIAL_RUMOR"},
    "overrides": {"rss:CNBC": "SECONDARY", "rss:White House": "PRIMARY"},
}


# --- priority assignment ---------------------------------------------------- #
def test_assign_priority_override_beats_default():
    assert alert_policy.assign_priority("rss:CNBC — Top News", PRIORITY_CFG) == "SECONDARY"


def test_assign_priority_type_default():
    assert alert_policy.assign_priority("rss:Trump — Truth Social", PRIORITY_CFG) == "PRIMARY"


def test_assign_priority_reddit_social():
    assert alert_policy.assign_priority("reddit:wallstreetbets", PRIORITY_CFG) == "SOCIAL_RUMOR"


def test_assign_priority_fallback_default():
    assert alert_policy.assign_priority("unknown:thing", PRIORITY_CFG) == "PRIMARY"


# --- dedup helpers ---------------------------------------------------------- #
def test_canonicalize_url_strips_tracking():
    a = alert_policy.canonicalize_url("https://www.example.com/story?utm_source=x&id=5")
    b = alert_policy.canonicalize_url("http://example.com/story/?id=5&utm_medium=y")
    assert a == b


def test_text_hash_matches_reposts():
    h1 = alert_policy.text_hash("Go out and BUY a Dell!!!")
    h2 = alert_policy.text_hash("go out and buy a dell")
    assert h1 == h2


# --- final confidence ------------------------------------------------------- #
def test_primary_high_stays_high():
    d = alert_policy.evaluate(_det(Confidence.HIGH), "PRIMARY", False, 0)
    assert d.confidence == Confidence.HIGH
    assert d.primary_source_found is True


def test_primary_low_stays_low():
    d = alert_policy.evaluate(_det(Confidence.LOW), "PRIMARY", True, 0)
    assert d.confidence == Confidence.LOW


def test_single_secondary_capped_at_medium():
    d = alert_policy.evaluate(_det(Confidence.HIGH), "SECONDARY", False, 1)
    assert d.confidence == Confidence.MEDIUM
    assert "single news source" in d.verification_status.lower()


def test_multiple_secondary_can_reach_high():
    d = alert_policy.evaluate(_det(Confidence.HIGH), "SECONDARY", False, 3)
    assert d.confidence == Confidence.HIGH
    assert "independent" in d.verification_status.lower()


def test_secondary_with_primary_corroboration_high():
    d = alert_policy.evaluate(_det(Confidence.HIGH), "SECONDARY", True, 1)
    assert d.confidence == Confidence.HIGH
    # A secondary report confirmed by a primary source reads as CONFIRMED.
    assert "confirmed" in d.verification_status.lower()
    assert "primary" in d.verification_status.lower()


def test_social_capped_at_low_with_warning():
    d = alert_policy.evaluate(_det(Confidence.HIGH), "SOCIAL_RUMOR", False, 0)
    assert d.confidence == Confidence.LOW
    assert alert_policy.social_warning_for(d)
    assert "unverified" in d.verification_status.lower()


# --- social early-warning alert gate (main.should_alert) -------------------- #
def test_should_alert_gate():
    import main
    medium_rank = Confidence.MEDIUM.rank()

    # Social post with a real stock-call (text HIGH) -> alerts despite LOW final.
    social_call = alert_policy.evaluate(_det(Confidence.HIGH), "SOCIAL_RUMOR", False, 0)
    assert main.should_alert(social_call, medium_rank) is True

    # Social post that is only a bare company mention (text LOW) -> stays quiet.
    social_bare = alert_policy.evaluate(_det(Confidence.LOW), "SOCIAL_RUMOR", False, 0)
    assert main.should_alert(social_bare, medium_rank) is False

    # A PRIMARY LOW mention -> below MEDIUM threshold, no alert.
    primary_low = alert_policy.evaluate(_det(Confidence.LOW), "PRIMARY", False, 0)
    assert main.should_alert(primary_low, medium_rank) is False

    # A PRIMARY HIGH -> alerts.
    primary_high = alert_policy.evaluate(_det(Confidence.HIGH), "PRIMARY", False, 0)
    assert main.should_alert(primary_high, medium_rank) is True
