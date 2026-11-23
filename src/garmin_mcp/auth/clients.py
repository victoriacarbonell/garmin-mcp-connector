"""How OAuth clients (Claude, VS Code, ChatGPT...) identify themselves.

The MCP specification lists three ways for a client to get a client_id. This
server supports the two automatic ones, so it works with old and new clients:

1. Client ID Metadata Documents (recommended by the spec). The client_id IS an
   https URL. We download the JSON document at that URL, which lists the
   client's name and redirect URIs. Nothing to store.

2. Dynamic Client Registration (deprecated in the spec, still widely used). The
   client POSTs its metadata to /register and gets a client_id back. A normal
   server stores that in a database; we instead return a client_id that is a
   signed token containing the metadata, so there is nothing to store either.

Registering a client grants nothing by itself: access still requires the owner
to log in with GitHub and approve that client on the consent screen.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import socket
import time
from typing import Any
from urllib.parse import urlparse

import httpx2
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import ValidationError

from .tokens import InvalidToken, TokenSigner

CLIENT_TOKEN_TYPE = "client"
METADATA_DOCUMENT_MAX_BYTES = 64 * 1024
METADATA_DOCUMENT_CACHE_SECONDS = 600
METADATA_DOCUMENT_TIMEOUT_SECONDS = 5.0

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})


def _is_loopback_match(registered: Any, requested: Any) -> bool:
    """RFC 8252 section 7.3: for a loopback redirect URI, any port is allowed.

    Native clients (Claude Code, desktop apps) listen on a random free port and
    register the URI without one, e.g. "http://localhost/callback". Everything
    except the port must still match exactly: scheme (http), host, path and query.
    """
    reg, req = urlparse(str(registered)), urlparse(str(requested))
    try:
        req.port  # raises ValueError on a malformed port
    except ValueError:
        return False
    if reg.scheme != "http" or req.scheme != "http":
        return False
    if "@" in reg.netloc or "@" in req.netloc:  # no user info in a redirect URI
        return False
    reg_host, req_host = _host_part(reg.netloc), _host_part(req.netloc)
    return (
        reg_host in _LOOPBACK_HOSTS
        and req_host == reg_host
        and (req.path, req.query, req.fragment) == (reg.path, reg.query, reg.fragment)
    )


def _host_part(netloc: str) -> str:
    """"localhost:43491" -> "localhost", "[::1]:8080" -> "[::1]", "127.0.0.1" -> "127.0.0.1"."""
    if netloc.startswith("["):
        return netloc[: netloc.index("]") + 1] if "]" in netloc else netloc
    return netloc.split(":", 1)[0]


class ClientInfo(OAuthClientInformationFull):
    """The SDK's client model, plus the loopback port rule of RFC 8252."""

    def validate_redirect_uri(self, redirect_uri: Any) -> Any:
        if redirect_uri is not None and any(
            _is_loopback_match(registered, redirect_uri) for registered in self.redirect_uris or ()
        ):
            return redirect_uri
        return super().validate_redirect_uri(redirect_uri)


# Fields of a registration that are worth keeping inside the signed client_id.
_REGISTERED_FIELDS = (
    "redirect_uris", "token_endpoint_auth_method", "grant_types", "response_types",
    "scope", "client_name", "client_uri", "application_type",
)  # fmt: skip


class ClientStore:
    def __init__(self, signer: TokenSigner, signing_key: str) -> None:
        self._signer = signer
        self._signing_key = signing_key.encode()
        self._documents: dict[str, tuple[float, OAuthClientInformationFull]] = {}

    # --- Dynamic Client Registration ---------------------------------------

    def register(self, client_info: OAuthClientInformationFull) -> None:
        """Turn the client_id the SDK generated into a signed, self-describing one.

        The SDK builds `client_info`, calls us, and then returns that same object
        to the client, so we replace the credentials on it in place.
        """
        metadata = client_info.model_dump(mode="json", exclude_none=True, include=set(_REGISTERED_FIELDS))
        client_info.client_id = self._signer.sign(CLIENT_TOKEN_TYPE, {"meta": metadata}, ttl_seconds=None)
        if client_info.client_secret is not None:
            client_info.client_secret = self._secret_for(client_info.client_id)

    def _secret_for(self, client_id: str) -> str:
        # Derived from the client_id and our key, so it can be recomputed on every
        # request instead of being stored.
        return hmac.new(self._signing_key, b"client-secret:" + client_id.encode(), hashlib.sha256).hexdigest()

    def _load_registered(self, client_id: str) -> OAuthClientInformationFull | None:
        try:
            claims = self._signer.verify(CLIENT_TOKEN_TYPE, client_id)
            metadata = dict(claims["meta"])
        except (InvalidToken, KeyError, TypeError, ValueError):
            return None
        secret = None
        if metadata.get("token_endpoint_auth_method") != "none":
            secret = self._secret_for(client_id)
        try:
            return ClientInfo.model_validate(
                {**metadata, "client_id": client_id, "client_secret": secret, "client_secret_expires_at": 0 if secret else None}
            )
        except ValidationError:
            return None

    # --- Client ID Metadata Documents --------------------------------------

    async def _load_metadata_document(self, url: str) -> OAuthClientInformationFull | None:
        cached = self._documents.get(url)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        if not _is_valid_metadata_url(url) or not await _resolves_to_public_address(url):
            return None
        try:
            async with httpx2.AsyncClient(timeout=METADATA_DOCUMENT_TIMEOUT_SECONDS, follow_redirects=False) as http:
                response = await http.get(url, headers={"Accept": "application/json"})
            if response.status_code != 200 or len(response.content) > METADATA_DOCUMENT_MAX_BYTES:
                return None
            document: Any = response.json()
        except (httpx2.HTTPError, ValueError):
            return None
        client = _client_from_document(url, document)
        if client is not None:
            # In-memory only and short-lived: it just saves refetching during one login.
            self._documents[url] = (time.monotonic() + METADATA_DOCUMENT_CACHE_SECONDS, client)
        return client

    # --- Lookup used by the provider ---------------------------------------

    async def get(self, client_id: str) -> OAuthClientInformationFull | None:
        if client_id.startswith("https://"):
            return await self._load_metadata_document(client_id)
        return self._load_registered(client_id)


def _is_valid_metadata_url(url: str) -> bool:
    """Spec rules for a URL-shaped client_id: https, with a path, nothing sneaky."""
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.path not in ("", "/")
        and not parsed.fragment
        and not parsed.username
        and not parsed.password
    )


async def _resolves_to_public_address(url: str) -> bool:
    """Refuse URLs that point inside our own network (SSRF protection).

    The client_id is chosen by whoever starts a login, so without this check
    anyone could make the server fetch internal addresses, such as the cloud
    metadata endpoint (169.254.169.254).
    """
    parsed = urlparse(url)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
        )
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)


def _client_from_document(url: str, document: Any) -> OAuthClientInformationFull | None:
    if not isinstance(document, dict):
        return None
    # The document must claim exactly the URL it was fetched from; otherwise one
    # site could pass off another site's document as its own.
    if document.get("client_id") != url or not document.get("client_name") or not document.get("redirect_uris"):
        return None
    # A metadata-document client has no shared secret with us: it is a public
    # client, protected by PKCE.
    if document.get("token_endpoint_auth_method", "none") != "none":
        return None
    try:
        return ClientInfo.model_validate(
            {
                "client_id": url,
                "client_name": document["client_name"],
                "redirect_uris": document["redirect_uris"],
                "client_uri": document.get("client_uri"),
                "scope": document.get("scope"),
                "grant_types": document.get("grant_types") or ["authorization_code", "refresh_token"],
                "response_types": document.get("response_types") or ["code"],
                "token_endpoint_auth_method": "none",
            }
        )
    except ValidationError:
        return None
