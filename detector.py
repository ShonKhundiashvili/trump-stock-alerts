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
    # News networks / wire services (usually publisher attributions, not tickers)
    "MSN", "BBC", "CNN", "NPR", "PBS", "NBC", "CBS", "ABC", "MSNBC", "UPI",
    "CNBC", "AFP", "PTI", "RT",
    # Geographies / orgs that collide with tickers
    "UK", "UAE", "NYC", "LA", "EU", "UN", "WHO", "IMF", "UAW", "DOD", "DHS",
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

# Regex signals that aren't fixed phrases. Matched on lowercased text.
# HIGH: ownership / explicit acquisition (incl. third-person "he bought").
OWNERSHIP_RE = [
    re.compile(r"\b(i|we|he|she|they)\s+(just\s+)?bought\b"),
    re.compile(r"\bafter\s+(he|she|they|we|i)\s+bought\b"),
    re.compile(r"\bbought\s+(it|in|shares|stock|into)\b"),
]
# MEDIUM: strong positive performance language ("up 250%", "soared 30%").
PERFORMANCE_RE = [
    re.compile(r"\bup\s+\d{1,4}(\.\d+)?\s*%"),
    re.compile(r"\b\d{1,4}(\.\d+)?\s*%\s+(gain|higher|up|surge)"),
    re.compile(r"\bsoar(ed|ing|s)?\b"),
    re.compile(r"\bskyrocket(ed|ing|s)?\b"),
    re.compile(r"\brecord\s+high\b"),
]
# BEARISH MEDIUM: negative performance ("down 20%", "plunged", "crashed").
BEARISH_PERFORMANCE_RE = [
    re.compile(r"\bdown\s+\d{1,4}(\.\d+)?\s*%"),
    re.compile(r"\b\d{1,4}(\.\d+)?\s*%\s+(loss|lower|drop|down|plunge)"),
    re.compile(r"\b(plunge|plunged|crash|crashed|tank|tanked|sink|sinks|tumble|tumbled|slump)\b"),
    re.compile(r"\brecord\s+low\b"),
]

EXCERPT_MAX = 320

# A buy/praise phrase must be within this many characters of a ticker mention to
# count toward that ticker's confidence (keeps long documents precise).
PROXIMITY_CHARS = 160

_PL_RANK = {PhraseLevel.NONE: 0, PhraseLevel.MEDIUM: 1, PhraseLevel.HIGH: 2}


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
        priority_tickers: Optional[Dict] = None,
        spacy_model: str = "en_core_web_sm",
        use_spacy: bool = True,
    ) -> None:
        self.resolver = resolver
        phrases = phrases or {}
        # Bullish (HIGH/MEDIUM) and bearish (BEARISH_HIGH/BEARISH_MEDIUM) phrase lists.
        self.high_phrases = [p.lower() for p in phrases.get("HIGH", [])]
        self.medium_phrases = [p.lower() for p in phrases.get("MEDIUM", [])]
        self.bearish_high_phrases = [p.lower() for p in phrases.get("BEARISH_HIGH", [])]
        self.bearish_medium_phrases = [p.lower() for p in phrases.get("BEARISH_MEDIUM", [])]
        self.watchlist = watchlist or {}
        priority_tickers = priority_tickers or {}
        self._sp500 = {t.upper() for t in priority_tickers.get("SP500", [])}
        self._nasdaq100 = {t.upper() for t in priority_tickers.get("NASDAQ100", [])}
        self._index_set = {t.upper() for t in priority_tickers.get("ALL", [])} \
            or (self._sp500 | self._nasdaq100)
        self._alias_terms = self._build_alias_terms()
        self._nlp = None
        if use_spacy:
            self._nlp = self._load_spacy(spacy_model)

    def _index_label(self, ticker: str) -> str:
        t = (ticker or "").upper()
        tags = []
        if t in self._sp500:
            tags.append("S&P 500")
        if t in self._nasdaq100:
            tags.append("Nasdaq-100")
        return ", ".join(tags)

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
        for rx in OWNERSHIP_RE:
            m = rx.search(normalized_lower)
            if m:
                return PhraseLevel.HIGH, m.group(0)
        for phrase in self.medium_phrases:
            if phrase in normalized_lower:
                return PhraseLevel.MEDIUM, phrase
        for rx in PERFORMANCE_RE:
            m = rx.search(normalized_lower)
            if m:
                return PhraseLevel.MEDIUM, m.group(0)
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
    def _all_phrase_hits(self, lowered: str):
        """Every phrase/regex match: (char_pos, level, text, direction).

        direction is "bullish" (buy/praise/positive perf) or "bearish"
        (sell/attack/negative perf), so the same machinery catches Trump talking
        a stock UP or DOWN.
        """
        hits = []

        def add_substrings(phrases, level, direction):
            for phrase in phrases:
                start = lowered.find(phrase)
                while start != -1:
                    hits.append((start, level, phrase, direction))
                    start = lowered.find(phrase, start + 1)

        def add_regex(regexes, level, direction):
            for rx in regexes:
                for m in rx.finditer(lowered):
                    hits.append((m.start(), level, m.group(0), direction))

        add_substrings(self.high_phrases, PhraseLevel.HIGH, "bullish")
        add_regex(OWNERSHIP_RE, PhraseLevel.HIGH, "bullish")
        add_substrings(self.bearish_high_phrases, PhraseLevel.HIGH, "bearish")
        add_substrings(self.medium_phrases, PhraseLevel.MEDIUM, "bullish")
        add_regex(PERFORMANCE_RE, PhraseLevel.MEDIUM, "bullish")
        add_substrings(self.bearish_medium_phrases, PhraseLevel.MEDIUM, "bearish")
        add_regex(BEARISH_PERFORMANCE_RE, PhraseLevel.MEDIUM, "bearish")
        return hits

    @staticmethod
    def _nearest_phrase(phrase_hits, positions):
        """Best phrase within PROXIMITY_CHARS of any of the ticker's positions.

        Returns (level, phrase_text, direction). Proximity keeps long documents
        precise (a phrase must be near the ticker mention, not just in the doc).
        """
        best_level, best_phrase, best_dir = PhraseLevel.NONE, None, "neutral"
        for ppos, plevel, ptext, pdir in phrase_hits:
            for tpos in positions:
                if abs(ppos - tpos) <= PROXIMITY_CHARS:
                    if _PL_RANK[plevel] > _PL_RANK[best_level]:
                        best_level, best_phrase, best_dir = plevel, ptext, pdir
                    break
            if best_level == PhraseLevel.HIGH:
                break
        return best_level, best_phrase, best_dir

    def detect(self, text: str) -> List[DetectionResult]:
        normalized = normalize(text)
        if not normalized:
            return []
        lowered = normalized.lower()

        phrase_hits = self._all_phrase_hits(lowered)
        has_phrase = bool(phrase_hits)
        excerpt = normalized[:EXCERPT_MAX]

        # ticker -> [CompanyMatch, detected_via, positions]
        resolved: Dict[str, list] = {}
        seen_queries: set[str] = set()

        def add(ticker: str, cm: "CompanyMatch", via: str, pos: int) -> None:
            if ticker not in resolved:
                resolved[ticker] = [cm, via, [pos]]
            else:
                resolved[ticker][2].append(pos)

        # 1. Cashtags ($DELL) — explicit ticker references.
        has_cashtag = False
        for m in CASHTAG_RE.finditer(normalized):
            has_cashtag = True
            cm = self.resolver.resolve_ticker_token(m.group(1))
            if cm and cm.ticker:
                add(cm.ticker, cm, "cashtag", m.start())

        stock_context = self._has_stock_context(lowered, has_cashtag)
        caps_run_tokens = self._caps_run_tokens(normalized)

        # 2. Bare uppercase ticker-like tokens (DELL, AMP, PLTR).
        #    Precision via INDEX MEMBERSHIP (S&P 500 / Nasdaq-100):
        #      - index tickers: trusted with buy/praise language nearby;
        #      - off-index tickers: require explicit stock context (a stray
        #        acronym like NRC/SMR/MMT won't qualify on praise alone);
        #      - <=2-char tickers (V, MU, GE): require explicit stock context
        #        even when in-index, since they collide with ordinary words.
        for m in UPPER_TOKEN_RE.finditer(normalized):
            token = m.group(1)
            if token in FALSE_POSITIVE_TOKENS or token in COMMON_WORD_TICKERS:
                continue
            if token in caps_run_tokens:
                continue
            # Skip tokens preceded by a number (units like "8 MMT", "100 MW").
            if re.search(r"\d\s*$", normalized[max(0, m.start() - 6):m.start()]):
                continue
            cm = self.resolver.resolve_ticker_token(token)
            if not (cm and cm.ticker and cm.strategy == "direct-ticker"):
                continue
            in_index = cm.ticker in self._index_set
            if len(token) <= 2:
                allow = stock_context or has_cashtag
            elif in_index:
                allow = stock_context or has_cashtag or has_phrase
            else:
                allow = stock_context or has_cashtag  # off-index needs a stock word
            if not allow:
                continue
            add(cm.ticker, cm, "ticker-token", m.start())

        # 3. Watchlist aliases (case-insensitive, word-boundary).
        for alias_lower, original in self._alias_terms:
            if alias_lower in seen_queries:
                continue
            for m in re.finditer(r"\b" + re.escape(alias_lower) + r"\b", lowered):
                seen_queries.add(alias_lower)
                cm = self.resolver.resolve(original)
                if cm and cm.ticker:
                    add(cm.ticker, cm, "watchlist", m.start())

        # 4. spaCy NER organizations (dynamic — beyond the watchlist).
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
                    # All-caps short tokens are tickers/acronyms — handled by the
                    # gated bare-ticker path, not here.
                    if name.isupper() and len(name) <= 5:
                        continue
                    seen_queries.add(name.lower())
                    cm = self.resolver.resolve(name)
                    if cm and cm.ticker and cm.resolution_confidence >= 90 and not cm.ambiguous:
                        if cm.ticker not in resolved:
                            resolved[cm.ticker] = [cm, "ner", [ent.start_char]]

        # Build detection results, scoring each ticker by the NEAREST phrase.
        results: List[DetectionResult] = []
        for ticker, (cm, via, positions) in resolved.items():
            phrase_level, matched_phrase, direction = self._nearest_phrase(phrase_hits, positions)
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
                    direction=direction if matched_phrase else "neutral",
                    in_index=self._index_label(cm.ticker),
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
