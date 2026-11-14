"""Configuration read from environment variables.

Everything sensitive comes in through environment variables: locally from a
`.env` file (ignored by git) and on Azure from Container Apps secrets. That way
the same code runs in both places unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """Configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    garmin_api_key: str
    port: int = 8000
    cache_ttl_seconds: float = 300.0

    def __repr__(self) -> str:
        # Never show the key, not even if someone prints the settings.
        return f"Settings(garmin_api_key='***', port={self.port}, cache_ttl_seconds={self.cache_ttl_seconds})"


def load_dotenv(path: str | Path = ".env") -> None:
    """Load a simple `.env` file (KEY=value) without overriding existing variables.

    It is 10 lines and saves one more dependency (python-dotenv).
    """
    file = Path(path)
    if not file.is_file():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_settings() -> Settings:
    api_key = os.environ.get("GARMIN_PERSONAL_API_KEY", "").strip()
    if not api_key:
        raise ConfigError(
            "GARMIN_PERSONAL_API_KEY is missing. Create the key in Garmin Connect Web "
            "(Personal API Key) and put it in the .env file."
        )
    try:
        port = int(os.environ.get("GARMIN_MCP_PORT", "8000"))
        cache_ttl = float(os.environ.get("GARMIN_MCP_CACHE_TTL", "300"))
    except ValueError as exc:
        raise ConfigError("GARMIN_MCP_PORT and GARMIN_MCP_CACHE_TTL must be numbers.") from exc
    return Settings(garmin_api_key=api_key, port=port, cache_ttl_seconds=cache_ttl)
