"""Optional LLM-based classification fallback.

This module is OPTIONAL. The whole system works without it. It is only invoked
*after* rule-based detection has already found a possible company/ticker, to
add a structured second opinion.

Rules (enforced by prompt + parsing):
  - The LLM is NEVER the only detector.
  - The LLM must NOT give financial advice — it only classifies the text.
  - Output must be strict JSON in the agreed schema.
  - If no API key is configured, the caller skips this step entirely.

Supports either OPENAI_API_KEY or ANTHROPIC_API_KEY. Both libraries are
optional imports; absence of the library simply disables the feature.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a strict text CLASSIFIER for a stock-mention monitoring tool. "
    "You DO NOT give financial advice. You DO NOT recommend buying or selling. "
    "You only classify whether the text mentions a publicly traded company or "
    "ticker and whether it contains buy/investment/stock-call language. "
    "Respond with STRICT JSON only — no prose, no markdown."
)

USER_TEMPLATE = """Classify the following text. Return JSON exactly in this schema:
{{
  "is_stock_related": true|false,
  "mentioned_companies": [
    {{"company_name": "...", "ticker": "...", "confidence": "HIGH|MEDIUM|LOW", "reason": "..."}}
  ],
  "buy_or_investment_language": true|false,
  "matched_phrase": "..."
}}

Text:
\"\"\"{text}\"\"\"
"""


@dataclass
class LLMCompany:
    company_name: str
    ticker: Optional[str]
    confidence: str
    reason: str


@dataclass
class LLMResult:
    is_stock_related: bool
    buy_or_investment_language: bool
    matched_phrase: Optional[str]
    mentioned_companies: List[LLMCompany] = field(default_factory=list)
    raw: Optional[str] = None


def _parse_json(content: str) -> Optional[dict]:
    """Extract the first JSON object from a model response."""
    content = content.strip()
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


class LLMClassifier:
    def __init__(
        self,
        openai_api_key: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
        openai_model: str = "gpt-4o-mini",
        anthropic_model: str = "claude-haiku-4-5-20251001",
    ) -> None:
        self.openai_api_key = openai_api_key
        self.anthropic_api_key = anthropic_api_key
        self.openai_model = openai_model
        self.anthropic_model = anthropic_model

    @property
    def enabled(self) -> bool:
        return bool(self.openai_api_key or self.anthropic_api_key)

    def classify(self, text: str) -> Optional[LLMResult]:
        """Return a structured classification, or None if unavailable/failed."""
        if not self.enabled:
            return None
        content: Optional[str] = None
        if self.anthropic_api_key:
            content = self._call_anthropic(text)
        if content is None and self.openai_api_key:
            content = self._call_openai(text)
        if content is None:
            return None

        data = _parse_json(content)
        if data is None:
            logger.warning("LLM returned non-JSON content; ignoring")
            return None
        companies = [
            LLMCompany(
                company_name=c.get("company_name", ""),
                ticker=(c.get("ticker") or None),
                confidence=str(c.get("confidence", "LOW")).upper(),
                reason=c.get("reason", ""),
            )
            for c in data.get("mentioned_companies", [])
            if isinstance(c, dict)
        ]
        return LLMResult(
            is_stock_related=bool(data.get("is_stock_related", False)),
            buy_or_investment_language=bool(data.get("buy_or_investment_language", False)),
            matched_phrase=data.get("matched_phrase") or None,
            mentioned_companies=companies,
            raw=content,
        )

    # ------------------------------------------------------------------ #
    def _call_anthropic(self, text: str) -> Optional[str]:
        try:
            import anthropic
        except Exception:
            logger.debug("anthropic library not installed; skipping")
            return None
        try:
            client = anthropic.Anthropic(api_key=self.anthropic_api_key)
            resp = client.messages.create(
                model=self.anthropic_model,
                max_tokens=600,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": USER_TEMPLATE.format(text=text)}],
            )
            return "".join(block.text for block in resp.content if block.type == "text")
        except Exception as exc:  # pragma: no cover - network
            logger.warning("Anthropic classification failed: %s", exc)
            return None

    def _call_openai(self, text: str) -> Optional[str]:
        try:
            from openai import OpenAI
        except Exception:
            logger.debug("openai library not installed; skipping")
            return None
        try:
            client = OpenAI(api_key=self.openai_api_key)
            resp = client.chat.completions.create(
                model=self.openai_model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": USER_TEMPLATE.format(text=text)},
                ],
            )
            return resp.choices[0].message.content
        except Exception as exc:  # pragma: no cover - network
            logger.warning("OpenAI classification failed: %s", exc)
            return None
