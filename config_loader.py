"""Configuration and environment loading.

Loads environment variables (via python-dotenv) and the JSON config files in
config/. No secrets are hardcoded anywhere; tokens always come from .env.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv should be installed
    def load_dotenv(*_args, **_kwargs):  # type: ignore
        return False


BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"

logger = logging.getLogger(__name__)


@dataclass
class Settings:
    """Runtime settings sourced from environment variables."""

    telegram_bot_token: Optional[str]
    telegram_chat_id: Optional[str]
    x_bearer_token: Optional[str]
    openai_api_key: Optional[str]
    anthropic_api_key: Optional[str]
    youtube_api_key: Optional[str]
    newsapi_key: Optional[str]
    kalshi_api_key: Optional[str]
    fmp_api_key: Optional[str]
    poll_seconds: int
    database_path: str
    log_level: str
    min_alert_confidence: str  # HIGH | MEDIUM | LOW — minimum to send a Telegram alert
    enable_feedback: bool      # ENABLE_TELEGRAM_FEEDBACK — inline buttons + commands
    channel_chats: Dict[str, str]  # channel name -> chat id (from TELEGRAM_CHAT_<NAME>)
    account_size: float        # for position-sizing in the trade plan (research only)
    risk_pct: float            # % of account risked per idea (research only)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openai_api_key or self.anthropic_api_key)


def load_settings(dotenv_path: Optional[str] = None) -> Settings:
    """Load settings from environment (and .env if present)."""
    load_dotenv(dotenv_path)

    def _int(name: str, default: int) -> int:
        raw = os.getenv(name)
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            logger.warning("Invalid int for %s=%r, using default %s", name, raw, default)
            return default

    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        x_bearer_token=os.getenv("X_BEARER_TOKEN") or None,
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        youtube_api_key=os.getenv("YOUTUBE_API_KEY") or None,
        newsapi_key=os.getenv("NEWSAPI_KEY") or None,
        kalshi_api_key=os.getenv("KALSHI_API_KEY") or None,
        fmp_api_key=os.getenv("FMP_API_KEY") or None,
        poll_seconds=_int("POLL_SECONDS", 60),
        database_path=os.getenv("DATABASE_PATH", "alerts.db"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        min_alert_confidence=(os.getenv("MIN_ALERT_CONFIDENCE", "MEDIUM") or "MEDIUM").upper(),
        enable_feedback=(os.getenv("ENABLE_TELEGRAM_FEEDBACK", "true") or "true").lower()
        not in ("0", "false", "no", "off", ""),
        channel_chats=_channel_chats_from_env(),
        account_size=float(os.getenv("ACCOUNT_SIZE", "10000") or 10000),
        risk_pct=float(os.getenv("RISK_PCT", "1.0") or 1.0),
    )


def _channel_chats_from_env() -> Dict[str, str]:
    """Map channel -> chat id from TELEGRAM_CHAT_<NAME> vars (excludes the plain
    TELEGRAM_CHAT_ID). e.g. TELEGRAM_CHAT_TRUMP=-100... -> {'trump': '-100...'}."""
    chats: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith("TELEGRAM_CHAT_") and key != "TELEGRAM_CHAT_ID" and value:
            chats[key[len("TELEGRAM_CHAT_"):].lower()] = value
    return chats


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        logger.warning("Config file %s not found; using default", path)
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse %s: %s; using default", path, exc)
        return default


def load_sources(config_dir: Path = CONFIG_DIR) -> Dict[str, Any]:
    return _load_json(config_dir / "sources.json", {})


def load_watchlist(config_dir: Path = CONFIG_DIR) -> Dict[str, Any]:
    return _load_json(config_dir / "watchlist.json", {})


def load_phrases(config_dir: Path = CONFIG_DIR) -> Dict[str, Any]:
    return _load_json(config_dir / "phrases.json", {"HIGH": [], "MEDIUM": []})


def load_scanner(config_dir: Path = CONFIG_DIR) -> Dict[str, Any]:
    return _load_json(
        config_dir / "scanner.json",
        {"min_score": 62, "min_rr": 1.5, "min_dollar_volume": 20000000,
         "max_finalists": 18},
    )


def load_topics(config_dir: Path = CONFIG_DIR) -> Dict[str, int]:
    """channel -> forum topic id (message_thread_id) within the main group."""
    data = _load_json(config_dir / "topics.json", {})
    out: Dict[str, int] = {}
    for k, v in data.items():
        if isinstance(v, int):
            out[k] = v
        elif isinstance(v, str) and v.lstrip("-").isdigit():
            out[k] = int(v)
    return out


def load_channels(config_dir: Path = CONFIG_DIR) -> Dict[str, Any]:
    return _load_json(
        config_dir / "channels.json",
        {"default_channel": "default", "routes": {}},
    )


def load_alerting(config_dir: Path = CONFIG_DIR) -> Dict[str, Any]:
    return _load_json(
        config_dir / "alerting.json",
        {
            "min_alert_score": 60,
            "send_low_confidence": False,
            "send_social_rumor": True,
            "social_rumor_min_score": 70,
            "respect_muted_sources": True,
            "respect_muted_companies": True,
            "max_age_hours": 48,
            "social_requires_corroboration": True,
            "penalize_uncorroborated": True,
            "require_corroboration": True,
            "min_independent_sources": 2,
            "ticker_cooldown_hours": 6,
            "relay_max_age_hours": 72,
            "cycle_summary": True,
        },
    )


def load_priority_tickers(config_dir: Path = CONFIG_DIR) -> Dict[str, Any]:
    return _load_json(
        config_dir / "priority_tickers.json",
        {"SP500": [], "NASDAQ100": [], "ALL": []},
    )


def load_source_priority(config_dir: Path = CONFIG_DIR) -> Dict[str, Any]:
    return _load_json(
        config_dir / "source_priority.json",
        {"defaults": {}, "overrides": {}, "DEFAULT": "PRIMARY"},
    )


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
