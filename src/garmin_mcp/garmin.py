"""Access to the Garmin Connect Personal API.

This layer wraps the official Garmin client (`garmin_personal_api`) and adds
what the client does not do on its own:

1. Renew the 24-hour token automatically (there are no refresh tokens: the key
   is simply exchanged for a new token).
2. Retry once if Garmin answers 401 for a token we believed was valid.
3. A small in-memory cache with expiry, so identical calls are not repeated
   when the assistant asks the same thing twice in a conversation.
4. "Clean" errors: the official client puts the response body in the error
   message, and that body could contain personal data. We drop it here so it
   can never end up in a log.

The official client is synchronous (it uses `requests`) and the MCP server is
asynchronous, so every call runs in a worker thread via `anyio.to_thread`.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import anyio
import requests
from garmin_personal_api import (
    GarminPersonalApiClient,
    GarminPersonalApiError,
    HttpError,
    PersonalApiAuthenticator,
)

# The official client logs every requested path at INFO level (with dates and
# IDs). That is not health data, but we do not need it: warnings and errors only.
logging.getLogger("garmin_personal_api").setLevel(logging.WARNING)

_MAX_CACHE_ENTRIES = 64


class GarminError(Exception):
    """Error talking to Garmin, with a safe message (no personal data)."""


class GarminService:
    def __init__(
        self,
        api_key: str,
        *,
        cache_ttl_seconds: float = 300.0,
        authenticator: Any | None = None,
        client_factory: Callable[[str], Any] = GarminPersonalApiClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api_key = api_key
        self._cache_ttl = cache_ttl_seconds
        self._clock = clock
        self._client_factory = client_factory
        if authenticator is None:
            # The official client ALWAYS stores the token in a file (it cannot be
            # turned off). We point it at a private temp directory (0700) that
            # disappears with the container, instead of ~/.config.
            token_dir = Path(tempfile.mkdtemp(prefix="garmin-mcp-"))
            authenticator = PersonalApiAuthenticator(
                bearer_token_cache_file=token_dir / "bearer-token.json"
            )
        self._authenticator = authenticator
        self._client: Any | None = None
        self._cache: dict[tuple, tuple[float, Any]] = {}
        # A single lock: calls to Garmin go one at a time. That is enough for one
        # user, avoids sharing the HTTP session across threads and keeps us polite
        # towards rate limits that Garmin does not document.
        self._lock = threading.Lock()

    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Call a method of the official client, e.g. `call("get_daily_sleep", "2026-11-01")`."""
        return await anyio.to_thread.run_sync(lambda: self._call_sync(method, args, kwargs))

    # --- internals ---------------------------------------------------------

    def _call_sync(self, method: str, args: tuple, kwargs: dict) -> Any:
        key = (method, args, tuple(sorted(kwargs.items())))
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > self._clock():
                return cached[1]
            try:
                result = self._request(method, args, kwargs)
            except HttpError as exc:
                raise GarminError(_safe_http_message(exc)) from None
            except GarminPersonalApiError:
                raise GarminError(
                    "The Garmin client rejected the request (invalid parameters or credentials)."
                ) from None
            except requests.RequestException as exc:
                raise GarminError(f"Could not reach Garmin ({type(exc).__name__}).") from None
            self._store(key, result)
            return result

    def _request(self, method: str, args: tuple, kwargs: dict) -> Any:
        self._refresh_client(force=False)
        try:
            return getattr(self._client, method)(*args, **kwargs)
        except HttpError as exc:
            if exc.status_code != 401:
                raise
        # 401: the token may have been revoked before expiring. Get a new one and
        # retry ONCE. If it fails again, the problem is the key itself.
        self._refresh_client(force=True)
        return getattr(self._client, method)(*args, **kwargs)

    def _refresh_client(self, *, force: bool) -> None:
        # obtain_token reuses the stored token while it has more than 1 hour left,
        # so calling it before every request is cheap and gives us automatic
        # renewal every ~23 hours with no logic of our own.
        token = self._authenticator.obtain_token(self._api_key, force_refresh=force)
        if self._client is None:
            self._client = self._client_factory(token.access_token)
        else:
            self._client.update_bearer_token(token.access_token)

    def _store(self, key: tuple, value: Any) -> None:
        if self._cache_ttl <= 0:
            return
        now = self._clock()
        self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        if len(self._cache) >= _MAX_CACHE_ENTRIES:
            self._cache.pop(next(iter(self._cache)))  # drop the oldest entry
        self._cache[key] = (now + self._cache_ttl, value)


def _safe_http_message(exc: HttpError) -> str:
    """Message with status code and path, but WITHOUT the response body."""
    path = urlparse(exc.url).path
    if exc.status_code == 401:
        hint = " The Garmin key may have expired or been deleted: create a new one."
    elif exc.status_code == 429:
        hint = " Garmin is rate limiting requests: wait a few minutes."
    else:
        hint = ""
    return f"Garmin returned HTTP {exc.status_code} for {exc.method} {path}.{hint}"
