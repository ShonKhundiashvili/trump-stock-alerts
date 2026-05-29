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
    poll_seconds: int
    database_path: str
    log_level: str
    min_alert_confidence: str  # HIGH | MEDIUM | LOW — minimum to send a Telegram alert

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
        poll_seconds=_int("POLL_SECONDS", 60),
        database_path=os.getenv("DATABASE_PATH", "alerts.db"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        min_alert_confidence=(os.getenv("MIN_ALERT_CONFIDENCE", "MEDIUM") or "MEDIUM").upper(),
    )


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
