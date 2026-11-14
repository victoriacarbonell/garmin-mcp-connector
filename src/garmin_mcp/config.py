"""Configuration read from environment variables.

Everything sensitive comes in through environment variables: locally from a
`.env` file (ignored by git) and on Azure from Container Apps secrets. That way
the same code runs in both places unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
MIN_SIGNING_KEY_LENGTH = 32


class ConfigError(Exception):
    """Configuration is missing or invalid."""


@dataclass(frozen=True)
class AuthSettings:
    """Everything the built-in OAuth authorization server needs."""

    public_url: str  # where clients reach this server, e.g. https://xyz.azurecontainerapps.io
    signing_key: str  # signs every token; rotating it logs every client out
    github_client_id: str
    github_client_secret: str
    github_allowed_user_id: int  # the ONLY GitHub account allowed to authorize clients
    # Overridable so tests can point at a fake GitHub. Leave the defaults in real use.
    github_oauth_url: str = "https://github.com/login/oauth"
    github_api_url: str = "https://api.github.com"

    @property
    def resource_url(self) -> str:
        """Canonical URI of the MCP endpoint: the audience of every access token."""
        return f"{self.public_url}/mcp"

    def __repr__(self) -> str:
        return f"AuthSettings(public_url={self.public_url!r}, github_allowed_user_id={self.github_allowed_user_id})"


@dataclass(frozen=True)
class Settings:
    garmin_api_key: str
    host: str = "127.0.0.1"
    port: int = 8000
    cache_ttl_seconds: float = 300.0
    auth: AuthSettings | None = None  # None = no authorization (loopback only)

    def __repr__(self) -> str:
        # Never show the key, not even if someone prints the settings.
        return f"Settings(garmin_api_key='***', host={self.host!r}, port={self.port}, auth={self.auth!r})"


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


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _load_auth() -> AuthSettings | None:
    if _env("GARMIN_MCP_AUTH_DISABLED").lower() in ("1", "true", "yes"):
        return None

    names = (
        "GARMIN_MCP_PUBLIC_URL",
        "GARMIN_MCP_SIGNING_KEY",
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
        "GITHUB_ALLOWED_USER_ID",
    )
    values = {name: _env(name) for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ConfigError(
            f"Authorization is enabled but these variables are missing: {', '.join(missing)}. "
            "Set them, or set GARMIN_MCP_AUTH_DISABLED=1 for local testing without authorization."
        )

    public_url = values["GARMIN_MCP_PUBLIC_URL"].rstrip("/")
    parsed = urlparse(public_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path or parsed.query:
        raise ConfigError("GARMIN_MCP_PUBLIC_URL must look like https://host[:port], with no path.")
    if parsed.scheme != "https" and parsed.hostname not in LOOPBACK_HOSTS:
        raise ConfigError("GARMIN_MCP_PUBLIC_URL must use https unless it points at localhost.")
    if len(values["GARMIN_MCP_SIGNING_KEY"]) < MIN_SIGNING_KEY_LENGTH:
        raise ConfigError(
            f"GARMIN_MCP_SIGNING_KEY must be at least {MIN_SIGNING_KEY_LENGTH} characters. "
            'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    try:
        allowed_user_id = int(values["GITHUB_ALLOWED_USER_ID"])
    except ValueError as exc:
        raise ConfigError("GITHUB_ALLOWED_USER_ID must be the numeric GitHub user id.") from exc

    return AuthSettings(
        public_url=public_url,
        signing_key=values["GARMIN_MCP_SIGNING_KEY"],
        github_client_id=values["GITHUB_CLIENT_ID"],
        github_client_secret=values["GITHUB_CLIENT_SECRET"],
        github_allowed_user_id=allowed_user_id,
        github_oauth_url=_env("GITHUB_OAUTH_URL", "https://github.com/login/oauth").rstrip("/"),
        github_api_url=_env("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
    )


def load_settings() -> Settings:
    api_key = _env("GARMIN_PERSONAL_API_KEY")
    if not api_key:
        raise ConfigError(
            "GARMIN_PERSONAL_API_KEY is missing. Create the key in Garmin Connect Web "
            "(Personal API Key) and put it in the .env file."
        )
    try:
        port = int(_env("GARMIN_MCP_PORT", "8000"))
        cache_ttl = float(_env("GARMIN_MCP_CACHE_TTL", "300"))
    except ValueError as exc:
        raise ConfigError("GARMIN_MCP_PORT and GARMIN_MCP_CACHE_TTL must be numbers.") from exc

    host = _env("GARMIN_MCP_HOST", "127.0.0.1")
    auth = _load_auth()
    if auth is None and host not in LOOPBACK_HOSTS:
        # The single most important guard in this file: health data must never be
        # reachable from the network without authorization.
        raise ConfigError(
            "Refusing to listen on a non-loopback host with authorization disabled. "
            "Remove GARMIN_MCP_AUTH_DISABLED or set GARMIN_MCP_HOST=127.0.0.1."
        )
    return Settings(
        garmin_api_key=api_key, host=host, port=port, cache_ttl_seconds=cache_ttl, auth=auth
    )
