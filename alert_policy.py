"""Source-priority + cross-source verification policy.

This layer sits on top of the (text-only) detector. It decides the FINAL alert
confidence by combining:
  - the text classification (HIGH/MEDIUM/LOW from detector.py), and
  - the source priority (PRIMARY / SECONDARY / SOCIAL_RUMOR), and
  - cross-source corroboration (does a PRIMARY source also carry this ticker?
    how many independent SECONDARY sources reported it recently?).

Rules (per spec):
  1. HIGH requires a PRIMARY source with direct text, OR multiple independent
     SECONDARY sources (corroboration).
  2. MEDIUM can come from a single reputable SECONDARY source.
  3. SOCIAL_RUMOR sources can only produce LOW, and are labelled unverified.
  4. Every alert shows its source priority + verification status.
  5. SOCIAL_RUMOR alerts include an explicit "unverified — check primary" warning.

The detector stays unchanged (and unit-tested independently); this module is the
provenance brain that main.py applies before alerting.
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict
from urllib.parse import urlparse

from models import Confidence, DetectionResult, SourcePriority

# How far back to look when corroborating a ticker across sources.
CORROBORATION_WINDOW_HOURS = 48

SOCIAL_WARNING = "⚠️ Unverified social signal — check primary source before acting."

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "ref", "ref_src", "cmp")


# --------------------------------------------------------------------------- #
# priority assignment
# --------------------------------------------------------------------------- #
def assign_priority(source_name: str, cfg: Dict) -> str:
    """Resolve a source name (e.g. 'rss:CNBC') to a SourcePriority value.

    Resolution order:
      1. Explicit override whose key is a prefix of the source name (longest first).
      2. Per-type default keyed by the part before ':' (e.g. 'rss', 'x', 'reddit').
      3. The 'DEFAULT' fallback (PRIMARY if unset).
    """
    overrides: Dict[str, str] = cfg.get("overrides", {})
    for key in sorted(overrides, key=len, reverse=True):
        if source_name.lower().startswith(key.lower()):
            return _norm(overrides[key])

    type_key = source_name.split(":", 1)[0]
    defaults: Dict[str, str] = cfg.get("defaults", {})
    if type_key in defaults:
        return _norm(defaults[type_key])

    return _norm(cfg.get("DEFAULT", "PRIMARY"))


def _norm(value: str) -> str:
    value = (value or "").upper().strip()
    return value if value in SourcePriority.__members__ else "PRIMARY"


# --------------------------------------------------------------------------- #
# dedup helpers
# --------------------------------------------------------------------------- #
def canonicalize_url(url: str) -> str:
    """Normalize a URL for cross-source dedup (drop scheme, tracking params, slash)."""
    if not url:
        return ""
    try:
        p = urlparse(url if "://" in url else "http://" + url)
    except ValueError:
        return url.strip().lower()
    host = (p.netloc or "").lower().lstrip("www.")
    path = (p.path or "").rstrip("/")
    query = ""
    if p.query:
        keep = [
            kv for kv in p.query.split("&")
            if kv and not any(kv.lower().startswith(t) for t in _TRACKING_PARAMS)
        ]
        query = "&".join(sorted(keep))
    base = f"{host}{path}"
    return f"{base}?{query}" if query else base


def text_hash(text: str) -> str:
    """Stable hash of normalized text, to catch identical reposts across sources."""
    norm = _NON_ALNUM.sub(" ", (text or "").lower()).strip()
    return hashlib.sha1(norm.encode("utf-8", "ignore")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# final confidence
# --------------------------------------------------------------------------- #
def _cap(conf: Confidence, ceiling: Confidence) -> Confidence:
    return conf if conf.rank() <= ceiling.rank() else ceiling


def evaluate(
    detection: DetectionResult,
    priority: str,
    primary_found: bool,
    secondary_count: int,
) -> DetectionResult:
    """Mutate `detection` with provenance fields + final confidence; return it.

    `secondary_count` = number of DISTINCT secondary sources (incl. the current
    one) that have flagged this ticker within the corroboration window.
    `primary_found` = a PRIMARY source has flagged this ticker recently.
    """
    text_conf = detection.text_confidence or detection.confidence
    detection.text_confidence = text_conf
    detection.source_priority = priority
    detection.primary_source_found = bool(primary_found)
    detection.corroborating_sources = secondary_count

    pr = _norm(priority)

    if pr == SourcePriority.PRIMARY.value:
        final = text_conf  # PRIMARY direct statement: trust the text classification
        detection.primary_source_found = True
        detection.verification_status = (
            "PRIMARY SOURCE — direct statement" if text_conf == Confidence.HIGH
            else "PRIMARY SOURCE"
        )

    elif pr == SourcePriority.SECONDARY.value:
        if primary_found:
            final = text_conf
            detection.verification_status = "CORROBORATED — primary source + news"
        elif secondary_count >= 2:
            final = text_conf  # multiple independent news sources -> can reach HIGH
            detection.verification_status = (
                f"CORROBORATED — {secondary_count} independent news sources"
            )
        else:
            final = _cap(text_conf, Confidence.MEDIUM)  # single news source: max MEDIUM
            detection.verification_status = "REPORTED — single news source (unconfirmed)"

    else:  # SOCIAL_RUMOR
        final = _cap(text_conf, Confidence.LOW)  # rumor layer: never above LOW
        detection.verification_status = "UNVERIFIED — social/rumor signal"

    detection.confidence = final
    return detection


def social_warning_for(detection: DetectionResult) -> str:
    if detection.source_priority == SourcePriority.SOCIAL_RUMOR.value:
        return SOCIAL_WARNING
    return ""
