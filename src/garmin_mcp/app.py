"""Assemble the web application: MCP endpoint plus, when enabled, authorization.

With authorization the same process plays two OAuth roles, which is what the
MCP specification expects from a self-contained server:

- Resource server: the /mcp endpoint. It rejects requests without a valid
  bearer token with a 401 whose WWW-Authenticate header points at
  /.well-known/oauth-protected-resource/mcp (RFC 9728), where clients discover
  which authorization server to use.
- Authorization server: /authorize, /token and /register, described at
  /.well-known/oauth-authorization-server (RFC 8414).

A client only needs the /mcp URL; it discovers everything else from there.
"""

from __future__ import annotations

from urllib.parse import urlparse

from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import AuthSettings as SdkAuthSettings
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from .auth.clients import ClientStore
from .auth.github import build_login_routes
from .auth.provider import GarminOAuthProvider
from .auth.tokens import TokenSigner
from .config import LOOPBACK_HOSTS, AuthSettings, Settings
from .server import create_mcp_server

# Stateless HTTP: every request stands alone, with no session kept in memory.
# Required for scale-to-zero, where a session would vanish on each cold start
# and clients would get "session not found" errors.
_HTTP_OPTIONS = {"stateless_http": True}


async def _health(request: Request) -> Response:
    """Unauthenticated liveness check for the hosting platform. Reveals nothing."""
    return PlainTextResponse("ok")


def build_app(settings: Settings) -> Starlette:
    if settings.auth is None:
        app = create_mcp_server().streamable_http_app(host=settings.host, **_HTTP_OPTIONS)
    else:
        app = _build_authorized_app(settings, settings.auth)
    app.router.routes.insert(0, Route("/healthz", _health, methods=["GET"]))
    return app


def _build_authorized_app(settings: Settings, auth: AuthSettings) -> Starlette:
    registration = ClientRegistrationOptions(enabled=True)
    # URLs are passed as plain strings on purpose: the SDK model keeps them exactly
    # as written, while building an AnyHttpUrl here would append a trailing slash.
    # Clients compare the issuer character by character, so that slash matters.
    sdk_auth = SdkAuthSettings(
        issuer_url=auth.public_url,  # type: ignore[arg-type]
        resource_server_url=auth.resource_url,  # type: ignore[arg-type]
        client_registration_options=registration,
        # Our provider checks the token audience itself when it verifies the
        # signature (see load_access_token), so the SDK's check would be a duplicate.
        validate_token_resource=False,
    )

    # Start from the metadata the SDK would publish and add what it leaves out.
    metadata = build_metadata(sdk_auth.issuer_url, None, registration, RevocationOptions(enabled=False))
    metadata.client_id_metadata_document_supported = True  # client_id may be an https URL
    metadata.authorization_response_iss_parameter_supported = True  # RFC 9207
    metadata.token_endpoint_auth_methods_supported = ["none", "client_secret_post", "client_secret_basic"]
    # Take the exact issuer string we publish and use it everywhere (tokens, `iss`).
    issuer = metadata.model_dump(mode="json")["issuer"]

    signer = TokenSigner(auth.signing_key, issuer)
    clients = ClientStore(signer, auth.signing_key)
    provider = GarminOAuthProvider(auth, signer, clients)

    mcp = create_mcp_server(auth_server_provider=provider, auth=sdk_auth)

    # DNS-rebinding protection only makes sense for servers on localhost, where a
    # web page could otherwise reach them through the browser. The SDK turns it
    # on by itself for loopback hosts; a public server is protected by tokens.
    on_loopback = urlparse(auth.public_url).hostname in LOOPBACK_HOSTS and settings.host in LOOPBACK_HOSTS
    app = mcp.streamable_http_app(host=settings.host if on_loopback else "0.0.0.0", **_HTTP_OPTIONS)

    # Inserted first so our metadata document wins over the SDK's default one.
    app.router.routes[0:0] = [
        Route(
            "/.well-known/oauth-authorization-server",
            cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"]),
            methods=["GET", "OPTIONS"],
        ),
        *build_login_routes(auth, provider, signer, clients, issuer),
    ]
    return app
