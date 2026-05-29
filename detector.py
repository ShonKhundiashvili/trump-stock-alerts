"""Detection pipeline.

Given a piece of text, the detector:
  1. Normalizes the text (smart quotes, whitespace).
  2. Detects cashtags ($DELL).
  3. Detects ticker-like tokens (DELL) when context suggests stocks.
  4. Detects company names via watchlist aliases + spaCy NER (organizations).
  5. Resolves companies/tokens to tickers via TickerResolver.
  6. Detects investment / stock-call phrases (config/phrases.json).
  7. Assigns HIGH / MEDIUM / LOW / NONE confidence.
  8. Returns one DetectionResult per distinct resolved company/ticker.

It is conservative: it filters out a stoplist of common false-positive acronyms
and never assigns HIGH confidence when ticker resolution is ambiguous.

This module CLASSIFIES only. It does not advise buying or selling anything.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

from models import CompanyMatch, Confidence, DetectionResult, PhraseLevel
from ticker_resolver import TickerResolver

logger = logging.getLogger(__name__)

# Common acronyms / tokens that look like tickers but almost never are, in
# Trump-related political text. Keeps "USA", "CEO", etc. from becoming alerts.
FALSE_POSITIVE_TOKENS = {
    "USA", "CEO", "CFO", "COO", "CTO", "GDP", "FBI", "IRS", "SEC", "AI", "EV",
    "TV", "DC", "GOP", "NY", "CA", "US", "NATO", "EU", "UN", "FED", "DOJ",
    "CIA", "NSA", "DHS", "FDA", "EPA", "WTO", "USD", "GMT", "EST", "PST",
    "OK", "USA.", "U.S.", "U.S", "DNC", "RNC", "VP", "POTUS", "FLOTUS", "PM",
}

# Common English words that also happen to be real tickers. On the *bare
# uppercase token* path (e.g. an all-caps heading) these cause false positives,
# so we ignore them there. Cashtags ($COST) and explicit context still work.
COMMON_WORD_TICKERS = {
    "COST", "ALL", "ARE", "FOR", "NEW", "NOW", "ONE", "OUT", "ANY", "GET",
    "BIG", "KEY", "CASH", "OPEN", "REAL", "LOVE", "CARS", "PLAY", "FUN",
    "GOOD", "FAST", "HOPE", "WELL", "BEST", "SAVE", "CARE", "LIFE", "HUGE",
    "FREE", "PLUS", "WORK", "LAND", "GOLD", "RUN", "WIN", "TRUE", "SAFE",
    "RICH", "FUND", "PLAN", "JOBS", "DEAL", "GROW", "RIDE", "STAY", "MOVE",
    "TECH", "DATA", "INFO", "NEXT", "EDGE", "PEAK", "RISE", "BOOM",
}

# Names that spaCy may tag as ORG but which are people / places / political
# entities, not tradable companies. Prevents e.g. "Trump" -> Trump Media (DJT)
# firing on every post that names him.
NER_NAME_STOPLIST = {
    "trump", "donald", "donald trump", "donald j. trump", "biden", "joe biden",
    "obama", "vance", "jd vance", "harris", "kamala harris", "putin", "xi",
    "america", "american", "americans", "united states", "u.s.", "us", "usa",
    "china", "russia", "europe", "congress", "senate", "house", "white house",
    "democrats", "republicans", "gop", "the white house", "washington",
    "border", "wall street", "nato", "fbi", "doj", "cia", "supreme court",
}

# Words whose presence signals we are in a "stock context", which is required
# before we trust a bare uppercase token like DELL as a ticker.
STOCK_CONTEXT_WORDS = {
    "buy", "buying", "bought", "stock", "stocks", "share", "shares", "invest",
    "investment", "investing", "ticker", "shareholder", "shareholders",
    "nasdaq", "nyse", "market", "equity", "ipo", "trading", "trade",
}

CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,6})(?:\.[A-Za-z]{1,2})?\b")
UPPER_TOKEN_RE = re.compile(r"\b([A-Z]{2,6})\b")
WORD_RE = re.compile(r"[a-z0-9']+")

EXCERPT_MAX = 320


def normalize(text: str) -> str:
    if not text:
        return ""
    replacements = {
        "‘": "'", "’": "'", "“": '"', "”": '"',
        "–": "-", "—": "-", " ": " ",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    text = re.sub(r"\s+", " ", text).strip()
    return text


class Detector:
    def __init__(
        self,
        resolver: TickerResolver,
        phrases: Optional[Dict[str, List[str]]] = None,
        watchlist: Optional[Dict] = None,
        spacy_model: str = "en_core_web_sm",
        use_spacy: bool = True,
    ) -> None:
        self.resolver = resolver
        phrases = phrases or {}
        self.high_phrases = [p.lower() for p in phrases.get("HIGH", [])]
        self.medium_phrases = [p.lower() for p in phrases.get("MEDIUM", [])]
        self.watchlist = watchlist or {}
        self._alias_terms = self._build_alias_terms()
        self._nlp = None
        if use_spacy:
            self._nlp = self._load_spacy(spacy_model)

    # ------------------------------------------------------------------ #
    def _build_alias_terms(self) -> List[Tuple[str, str]]:
        """Return (alias_lower, original_alias) sorted longest-first for matching."""
        terms: List[Tuple[str, str]] = []
        for key, entry in self.watchlist.items():
            aliases = list(entry.get("aliases", [])) + [key]
            for alias in aliases:
                if alias and len(alias) >= 2:
                    terms.append((alias.lower(), alias))
        terms.sort(key=lambda t: len(t[0]), reverse=True)
        return terms

    def _load_spacy(self, model: str):
        try:
            import spacy
        except Exception:
            logger.warning("spaCy not installed; falling back to watchlist/cashtag detection")
            return None
        try:
            return spacy.load(model)
        except Exception:
            logger.warning(
                "spaCy model %r not available; run `python -m spacy download %s`. "
                "Falling back to watchlist/cashtag detection.", model, model,
            )
            return None

    # ------------------------------------------------------------------ #
    # phrase classification
    # ------------------------------------------------------------------ #
    def match_phrase(self, normalized_lower: str) -> Tuple[PhraseLevel, Optional[str]]:
        for phrase in self.high_phrases:
            if phrase in normalized_lower:
                return PhraseLevel.HIGH, phrase
        for phrase in self.medium_phrases:
            if phrase in normalized_lower:
                return PhraseLevel.MEDIUM, phrase
        return PhraseLevel.NONE, None

    # ------------------------------------------------------------------ #
    def _has_stock_context(self, normalized_lower: str, has_cashtag: bool) -> bool:
        if has_cashtag:
            return True
        words = set(WORD_RE.findall(normalized_lower))
        return bool(words & STOCK_CONTEXT_WORDS)

    @staticmethod
    def _caps_run_tokens(normalized: str) -> set:
        """All-caps words that sit next to another all-caps word.

        These come from headings / shouted emphasis (e.g. "LOWER COST FOR ALL")
        rather than intentional ticker references, so we exclude them from the
        bare-uppercase-token path.
        """
        words = normalized.split()

        def caps(w: str) -> bool:
            w = w.strip(".,!?:;\"'()[]")
            return len(w) >= 2 and w.isalpha() and w.isupper()

        run: set = set()
        for i, w in enumerate(words):
            if not caps(w):
                continue
            prev_caps = i > 0 and caps(words[i - 1])
            next_caps = i < len(words) - 1 and caps(words[i + 1])
            if prev_caps or next_caps:
                run.add(w.strip(".,!?:;\"'()[]").upper())
        return run

    # ------------------------------------------------------------------ #
    def detect(self, text: str) -> List[DetectionResult]:
        normalized = normalize(text)
        if not normalized:
            return []
        lowered = normalized.lower()

        phrase_level, matched_phrase = self.match_phrase(lowered)
        excerpt = normalized[:EXCERPT_MAX]

        # ticker -> (CompanyMatch, detected_via)
        resolved: Dict[str, Tuple[CompanyMatch, str]] = {}
        # track names already resolved so we don't double-process
        seen_queries: set[str] = set()

        # 1. Cashtags ($DELL) — explicit ticker references.
        cashtags = CASHTAG_RE.findall(normalized)
        has_cashtag = bool(cashtags)
        for sym in cashtags:
            cm = self.resolver.resolve_ticker_token(sym)
            if cm and cm.ticker:
                resolved.setdefault(cm.ticker, (cm, "cashtag"))

        stock_context = self._has_stock_context(lowered, has_cashtag)
        caps_run_tokens = self._caps_run_tokens(normalized)

        # 2. Bare uppercase ticker-like tokens (DELL) — only in stock context.
        if stock_context:
            for token in UPPER_TOKEN_RE.findall(normalized):
                if token in FALSE_POSITIVE_TOKENS or token in COMMON_WORD_TICKERS:
                    continue
                # Skip tokens that are part of an all-caps heading / emphasis run
                # (e.g. "DELIVERING A LOWER COST"), which aren't real tickers.
                if token in caps_run_tokens:
                    continue
                cm = self.resolver.resolve_ticker_token(token)
                # Only trust it if it actually exists in our universe.
                if cm and cm.ticker and cm.strategy == "direct-ticker":
                    resolved.setdefault(cm.ticker, (cm, "ticker-token"))

        # 3. Watchlist aliases (case-insensitive, word-boundary).
        for alias_lower, original in self._alias_terms:
            if alias_lower in seen_queries:
                continue
            pattern = r"\b" + re.escape(alias_lower) + r"\b"
            if re.search(pattern, lowered):
                seen_queries.add(alias_lower)
                cm = self.resolver.resolve(original)
                if cm and cm.ticker:
                    resolved.setdefault(cm.ticker, (cm, "watchlist"))

        # 4. spaCy NER organizations (dynamic — works beyond the watchlist).
        if self._nlp is not None:
            try:
                doc = self._nlp(normalized)
            except Exception as exc:  # pragma: no cover
                logger.debug("spaCy processing failed: %s", exc)
                doc = None
            if doc is not None:
                for ent in doc.ents:
                    if ent.label_ not in ("ORG", "PRODUCT"):
                        continue
                    name = ent.text.strip().strip(".,'\"")
                    if not name or name.lower() in seen_queries:
                        continue
                    if name.upper() in FALSE_POSITIVE_TOKENS:
                        continue
                    if name.lower() in NER_NAME_STOPLIST:
                        continue
                    seen_queries.add(name.lower())
                    cm = self.resolver.resolve(name)
                    # Require a stronger match for open-ended NER hits than for
                    # watchlist/cashtag, to avoid fuzzy false positives.
                    if cm and cm.ticker and cm.resolution_confidence >= 90 and not cm.ambiguous:
                        # don't clobber a higher-priority (watchlist) match
                        if cm.ticker not in resolved:
                            resolved[cm.ticker] = (cm, "ner")

        # Build detection results.
        results: List[DetectionResult] = []
        for ticker, (cm, via) in resolved.items():
            confidence = self._assign_confidence(cm, phrase_level)
            if confidence == Confidence.NONE:
                continue
            results.append(
                DetectionResult(
                    company_name=cm.company_name,
                    ticker=cm.ticker,
                    candidate_tickers=[c.ticker for c in cm.candidates],
                    confidence=confidence,
                    ticker_resolution_confidence=round(cm.resolution_confidence, 1),
                    matched_phrase=matched_phrase,
                    text_excerpt=excerpt,
                    ambiguous=cm.ambiguous,
                    detected_via=via,
                )
            )
        return results

    # ------------------------------------------------------------------ #
    def _assign_confidence(self, cm: CompanyMatch, phrase_level: PhraseLevel) -> Confidence:
        """Combine ticker resolution + phrase strength into overall confidence.

        HIGH   : confident ticker + explicit buy/invest language + not ambiguous.
        MEDIUM : company detected + positive business language, OR a HIGH phrase
                 that is undercut by ambiguous ticker resolution.
        LOW    : company/ticker detected but no investment wording.
        NONE   : nothing useful detected.
        """
        if not cm.resolved:
            return Confidence.NONE

        if phrase_level == PhraseLevel.HIGH:
            if cm.ambiguous:
                return Confidence.MEDIUM  # never HIGH when resolution is ambiguous
            return Confidence.HIGH
        if phrase_level == PhraseLevel.MEDIUM:
            return Confidence.MEDIUM
        return Confidence.LOW
