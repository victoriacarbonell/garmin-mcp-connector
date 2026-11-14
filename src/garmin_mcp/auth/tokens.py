"""Signed, self-contained tokens (JWT, HS256).

Why signed tokens instead of a table of issued tokens: the container scales to
zero, so anything kept in memory disappears on the next cold start, and we do
not want a database. A signed token carries its own data and expiry; the server
only needs the signing key to check that it issued the token and that nobody
altered it.

The trade-off: a single token cannot be revoked. To cut off every client at
once, rotate GARMIN_MCP_SIGNING_KEY.

Every token has a `typ` claim and is only accepted where that type is expected.
Without it, a refresh token could be replayed as an access token, or a consent
form token as an authorization code, because they share the same key.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import jwt

ALGORITHM = "HS256"


class InvalidToken(Exception):
    """The token is malformed, expired, tampered with or of the wrong type."""


class TokenSigner:
    def __init__(self, signing_key: str, issuer: str) -> None:
        self._key = signing_key
        self._issuer = issuer

    def sign(self, typ: str, claims: dict[str, Any], ttl_seconds: int | None) -> str:
        """Sign `claims` as a token of type `typ`. `ttl_seconds=None` means no expiry."""
        now = int(time.time())
        payload = {**claims, "typ": typ, "iss": self._issuer, "iat": now, "jti": secrets.token_urlsafe(12)}
        if ttl_seconds is not None:
            payload["exp"] = now + ttl_seconds
        return jwt.encode(payload, self._key, algorithm=ALGORITHM)

    def verify(self, typ: str, token: str, *, audience: str | None = None) -> dict[str, Any]:
        """Return the claims of a valid token of type `typ`, or raise InvalidToken."""
        try:
            claims = jwt.decode(
                token,
                self._key,
                algorithms=[ALGORITHM],  # never trust the algorithm named inside the token
                issuer=self._issuer,
                audience=audience,
                options={"require": ["iss", "iat", "jti", "typ"], "verify_aud": audience is not None},
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(type(exc).__name__) from None
        if claims.get("typ") != typ:
            raise InvalidToken("wrong token type")
        return claims
