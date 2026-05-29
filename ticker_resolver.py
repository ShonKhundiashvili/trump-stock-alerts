"""Dynamic company-name / ticker resolution.

Given a detected name ("Dell", "Intel", "Tesla") or a ticker-like token
("DELL", "$NVDA"), resolve it to one or more likely public tickers.

Resolution strategies, in priority order:
  1. Exact matches from config/watchlist.json (priority aliases / overrides).
  2. Direct ticker / cashtag match against the local stock universe.
  3. Fuzzy search against company names in the local stock universe (rapidfuzz).
  4. Optional online lookup via yfinance (NEVER required; off the hot path).

Preferences:
  - Prefer exact watchlist aliases over fuzzy matches.
  - Prefer US-listed equities.
  - Lower confidence (and flag `ambiguous`) when several companies tie.
  - Avoid matching very short words unless they are known tickers or aliases.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

from models import CompanyMatch, TickerCandidate

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None  # type: ignore

try:
    from rapidfuzz import fuzz, process
except Exception:  # pragma: no cover
    fuzz = None  # type: ignore
    process = None  # type: ignore

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_UNIVERSE = BASE_DIR / "data" / "stock_universe.csv"

# Tunables
FUZZY_MIN_SCORE = 82.0        # below this we do not claim a confident match
FUZZY_AMBIGUITY_GAP = 5.0     # if 2nd best is within this of the best -> ambiguous
WATCHLIST_CONFIDENCE = 98.0   # confidence assigned to an exact watchlist alias hit
DIRECT_TICKER_CONFIDENCE = 99.0
SHORT_NAME_MIN_LEN = 3        # names shorter than this need to be known tickers/aliases


class TickerResolver:
    def __init__(
        self,
        watchlist: Optional[Dict] = None,
        universe_path: Path = DEFAULT_UNIVERSE,
        enable_online: bool = False,
    ) -> None:
        self.watchlist = watchlist or {}
        self.universe_path = universe_path
        self.enable_online = enable_online

        # alias (lower) -> (ticker, canonical company name)
        self._alias_index: Dict[str, tuple[str, str]] = {}
        self._build_alias_index()

        # universe data
        self._universe: List[dict] = []
        self._ticker_index: Dict[str, dict] = {}
        self._company_names: List[str] = []
        self._name_to_row: Dict[str, dict] = {}
        self._load_universe()

    # ------------------------------------------------------------------ #
    # index building
    # ------------------------------------------------------------------ #
    def _build_alias_index(self) -> None:
        for _key, entry in self.watchlist.items():
            ticker = str(entry.get("ticker", "")).upper().strip()
            company = entry.get("company_name") or entry.get("name") or _key
            aliases = list(entry.get("aliases", []))
            aliases.append(_key)
            if company:
                aliases.append(company)
            for alias in aliases:
                if alias:
                    self._alias_index[alias.lower().strip()] = (ticker, company)

    def _load_universe(self) -> None:
        if pd is None:
            logger.warning("pandas not available; stock universe disabled")
            return
        if not self.universe_path.exists():
            logger.warning("Stock universe not found at %s", self.universe_path)
            return
        df = pd.read_csv(self.universe_path).fillna("")
        for _, row in df.iterrows():
            rec = {
                "ticker": str(row.get("ticker", "")).upper().strip(),
                "company_name": str(row.get("company_name", "")).strip(),
                "exchange": str(row.get("exchange", "")).strip(),
                "country": str(row.get("country", "")).strip(),
                "asset_type": str(row.get("asset_type", "")).strip(),
            }
            if not rec["ticker"]:
                continue
            self._universe.append(rec)
            self._ticker_index[rec["ticker"]] = rec
            if rec["company_name"]:
                self._company_names.append(rec["company_name"])
                self._name_to_row[rec["company_name"]] = rec
        logger.info("Loaded %d tickers from stock universe", len(self._universe))

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _candidate_from_row(self, rec: dict, score: float, strategy: str) -> TickerCandidate:
        return TickerCandidate(
            ticker=rec["ticker"],
            company_name=rec["company_name"],
            score=score,
            exchange=rec.get("exchange", ""),
            country=rec.get("country", ""),
            asset_type=rec.get("asset_type", ""),
            strategy=strategy,
        )

    def _is_us_equity(self, rec: dict) -> bool:
        return rec.get("country", "").upper() in ("US", "USA", "UNITED STATES", "") and (
            rec.get("asset_type", "").lower() in ("", "equity", "stock", "common stock")
        )

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def resolve_ticker_token(self, token: str) -> Optional[CompanyMatch]:
        """Resolve a literal ticker/cashtag token like 'DELL' or '$NVDA'."""
        sym = token.lstrip("$").upper().strip()
        if not sym:
            return None
        rec = self._ticker_index.get(sym)
        if rec:
            cand = self._candidate_from_row(rec, DIRECT_TICKER_CONFIDENCE, "direct-ticker")
            return CompanyMatch(
                query=token,
                ticker=sym,
                company_name=rec["company_name"],
                resolution_confidence=DIRECT_TICKER_CONFIDENCE,
                candidates=[cand],
                ambiguous=False,
                strategy="direct-ticker",
            )
        # Even if not in our universe, a cashtag is an explicit ticker reference.
        return CompanyMatch(
            query=token,
            ticker=sym,
            company_name=None,
            resolution_confidence=85.0,
            candidates=[TickerCandidate(sym, "", 85.0, strategy="cashtag")],
            ambiguous=False,
            strategy="cashtag",
        )

    def resolve(self, name: str) -> CompanyMatch:
        """Resolve a company name / token to a ticker with confidence + candidates."""
        query = (name or "").strip()
        if not query:
            return CompanyMatch(query=name, ticker=None, company_name=None,
                                resolution_confidence=0.0, candidates=[])

        lowered = query.lower()

        # 1. Exact watchlist alias (highest priority).
        if lowered in self._alias_index:
            ticker, company = self._alias_index[lowered]
            rec = self._ticker_index.get(ticker, {})
            company = company or rec.get("company_name") or query
            cand = TickerCandidate(ticker, company, WATCHLIST_CONFIDENCE,
                                   exchange=rec.get("exchange", ""),
                                   country=rec.get("country", ""),
                                   asset_type=rec.get("asset_type", ""),
                                   strategy="watchlist")
            return CompanyMatch(
                query=query, ticker=ticker, company_name=company,
                resolution_confidence=WATCHLIST_CONFIDENCE, candidates=[cand],
                ambiguous=False, strategy="watchlist",
            )

        # 2. Direct ticker token (uppercase symbol that exists).
        if query.isupper() and query in self._ticker_index:
            return self.resolve_ticker_token(query)  # type: ignore[return-value]

        # Guard against very short names that are neither aliases nor known tickers.
        if len(query) < SHORT_NAME_MIN_LEN:
            return CompanyMatch(query=query, ticker=None, company_name=None,
                                resolution_confidence=0.0, candidates=[], ambiguous=False)

        # 3. Fuzzy match against universe company names.
        fuzzy = self._fuzzy_match(query)
        if fuzzy.resolved:
            return fuzzy

        # 4. Optional online lookup (never on the required hot path).
        if self.enable_online:
            online = self._online_lookup(query)
            if online and online.resolved:
                return online

        return fuzzy  # unresolved match carries candidates (if any) + low confidence

    # ------------------------------------------------------------------ #
    def _fuzzy_match(self, query: str) -> CompanyMatch:
        if fuzz is None or not self._company_names:
            return CompanyMatch(query=query, ticker=None, company_name=None,
                                resolution_confidence=0.0, candidates=[])

        # Get top fuzzy matches by token_set_ratio.
        results = process.extract(
            query, self._company_names, scorer=fuzz.token_set_ratio, limit=8
        )
        # results: list of (matched_name, score, index)
        candidates: List[TickerCandidate] = []
        seen_tickers = set()
        for matched_name, score, _idx in results:
            rec = self._name_to_row.get(matched_name)
            if not rec:
                continue
            # US-equity preference: a small bonus so US listings outrank dupes.
            adj = float(score) + (1.5 if self._is_us_equity(rec) else 0.0)
            if rec["ticker"] in seen_tickers:
                continue
            seen_tickers.add(rec["ticker"])
            candidates.append(self._candidate_from_row(rec, min(adj, 100.0), "fuzzy"))

        candidates.sort(key=lambda c: c.score, reverse=True)
        if not candidates:
            return CompanyMatch(query=query, ticker=None, company_name=None,
                                resolution_confidence=0.0, candidates=[])

        best = candidates[0]
        if best.score < FUZZY_MIN_SCORE:
            # No confident match. Return candidates for transparency, ticker=None.
            return CompanyMatch(
                query=query, ticker=None, company_name=None,
                resolution_confidence=best.score, candidates=candidates[:5],
                ambiguous=len(candidates) > 1, strategy="fuzzy",
            )

        # Ambiguity: is the runner-up nearly as good?
        ambiguous = (
            len(candidates) > 1
            and (best.score - candidates[1].score) < FUZZY_AMBIGUITY_GAP
        )
        confidence = best.score
        if ambiguous:
            confidence = min(confidence, 80.0)  # never claim high confidence when tied

        return CompanyMatch(
            query=query, ticker=best.ticker, company_name=best.company_name,
            resolution_confidence=confidence, candidates=candidates[:5],
            ambiguous=ambiguous, strategy="fuzzy",
        )

    # ------------------------------------------------------------------ #
    def _online_lookup(self, query: str) -> Optional[CompanyMatch]:
        """Best-effort yfinance lookup. Optional, network-dependent, never required."""
        try:
            import yfinance as yf  # lazy import; optional dependency at runtime
        except Exception:
            return None
        try:
            search = yf.Search(query, max_results=5)  # type: ignore[attr-defined]
            quotes = getattr(search, "quotes", []) or []
        except Exception as exc:  # pragma: no cover - network
            logger.debug("yfinance lookup failed for %r: %s", query, exc)
            return None

        candidates: List[TickerCandidate] = []
        for q in quotes:
            symbol = (q.get("symbol") or "").upper()
            name = q.get("shortname") or q.get("longname") or ""
            if not symbol:
                continue
            candidates.append(TickerCandidate(symbol, name, 88.0, strategy="yfinance"))
        if not candidates:
            return None
        best = candidates[0]
        return CompanyMatch(
            query=query, ticker=best.ticker, company_name=best.company_name,
            resolution_confidence=best.score, candidates=candidates,
            ambiguous=len(candidates) > 1, strategy="yfinance",
        )
