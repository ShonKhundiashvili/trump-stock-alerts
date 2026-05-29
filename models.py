"""Typed data models shared across the trump-stock-alerts system.

These models are intentionally lightweight (dataclasses + a couple of pydantic
models) so they can be passed between source adapters, the detector, the
database layer, and the Telegram alerter without coupling those layers.

This system CLASSIFIES information only. It does not give financial advice and
it does not trade. Every alert links back to the original source so a human can
verify it manually.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


class Confidence(str, enum.Enum):
    """Overall confidence that a piece of text is a stock-related Trump mention."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"

    def rank(self) -> int:
        return {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}[self.value]


class PhraseLevel(str, enum.Enum):
    """How strong the investment/stock-call language is."""

    HIGH = "HIGH"      # explicit buy / investment language
    MEDIUM = "MEDIUM"  # positive business language, but not a direct buy call
    NONE = "NONE"      # no investment-flavored language


class SourcePriority(str, enum.Enum):
    """Provenance tier of a source (how trustworthy / how close to the source)."""

    PRIMARY = "PRIMARY"            # Trump's own posts, official releases, transcripts
    SECONDARY = "SECONDARY"        # reputable news reporting on what Trump said
    SOCIAL_RUMOR = "SOCIAL_RUMOR"  # Reddit / reposts — early warning, unverified

    def rank(self) -> int:
        return {"SOCIAL_RUMOR": 0, "SECONDARY": 1, "PRIMARY": 2}[self.value]


@dataclass
class SourceItem:
    """A single normalized item fetched from any source adapter."""

    source: str                 # adapter / source name (e.g. "x:realDonaldTrump")
    source_item_id: str         # stable unique id within that source
    url: str                    # original source link (always present for verification)
    text: str                   # raw text content
    timestamp: str              # ISO8601 timestamp of the item (best effort)
    title: Optional[str] = None
    priority: str = "PRIMARY"   # SourcePriority value, stamped by the adapter
    channel: str = "default"    # routing bucket (e.g. trump / markets / contracts)
    relay: bool = False         # forward as-is (prediction markets) vs run detector
    ticker: Optional[str] = None  # optional ticker hint set by structured sources
    canonical_url: Optional[str] = None
    text_hash: Optional[str] = None

    def fingerprint(self) -> str:
        return f"{self.source}::{self.source_item_id}"


@dataclass
class TickerCandidate:
    """A single candidate ticker produced by the resolver."""

    ticker: str
    company_name: str
    score: float                # 0-100 fuzzy / match score
    exchange: str = ""
    country: str = ""
    asset_type: str = ""
    strategy: str = ""          # how it was matched (watchlist/cashtag/fuzzy/...)

    def __str__(self) -> str:
        return f"{self.ticker} ({self.company_name}) [{self.score:.0f}]"


@dataclass
class CompanyMatch:
    """Result of resolving a detected name/token to a ticker."""

    query: str                       # the detected text we tried to resolve
    ticker: Optional[str]            # best ticker, or None if not confident
    company_name: Optional[str]
    resolution_confidence: float     # 0-100
    candidates: List[TickerCandidate] = field(default_factory=list)
    ambiguous: bool = False
    strategy: str = ""

    @property
    def resolved(self) -> bool:
        return self.ticker is not None


@dataclass
class DetectionResult:
    """One detected company/ticker mention with its classification."""

    company_name: Optional[str]
    ticker: Optional[str]
    candidate_tickers: List[str]
    confidence: Confidence
    ticker_resolution_confidence: float
    matched_phrase: Optional[str]
    text_excerpt: str
    ambiguous: bool = False
    detected_via: str = ""           # cashtag / ticker-token / ner / watchlist
    direction: str = "bullish"       # bullish | bearish | neutral
    in_index: str = ""               # "S&P 500", "Nasdaq-100", both, or "" (off-index)
    llm_used: bool = False
    llm_reason: Optional[str] = None
    # Cross-source / provenance fields (filled by alert_policy after detection).
    text_confidence: Optional["Confidence"] = None  # original text-only confidence
    source_priority: str = "PRIMARY"
    verification_status: str = ""
    primary_source_found: bool = False
    corroborating_sources: int = 0
    trade_note: str = ""             # price-reaction + research trade plan (appended to alert)

    def is_alertable(self) -> bool:
        return self.confidence in (Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW)


def now_iso() -> str:
    """UTC timestamp in ISO8601 (used for created_at columns)."""
    return datetime.now(timezone.utc).isoformat()
