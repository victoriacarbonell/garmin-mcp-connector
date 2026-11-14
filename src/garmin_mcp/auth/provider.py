"""The OAuth logic the MCP SDK calls into.

The SDK already implements the HTTP endpoints (/authorize, /token, /register)
and their validation (PKCE check, redirect URI check, client authentication).
It delegates the decisions to a "provider", which is this class:

    /register   -> register_client
    /authorize  -> authorize                 (we send the browser to GitHub)
    /token      -> load_authorization_code + exchange_authorization_code
                   load_refresh_token      + exchange_refresh_token
    every MCP request -> load_access_token   (is this bearer token ours and valid?)

The login itself (GitHub and the consent screen) lives in `github.py`; when the
owner approves, it calls `issue_authorization_code` here.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from ..config import AuthSettings
from .clients import ClientStore
from .tokens import InvalidToken, TokenSigner

LOGIN_TTL_SECONDS = 600  # time to complete the GitHub login
CONSENT_TTL_SECONDS = 300  # time to press "Allow" on the consent screen
CODE_TTL_SECONDS = 60  # the client exchanges the code immediately
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600  # renewed on every use (sliding window)

# The fields that travel from /authorize through GitHub and the consent screen
# into the authorization code, always inside signed tokens.
GRANT_FIELDS = ("client_id", "redirect_uri", "redirect_uri_explicit", "code_challenge", "state", "scopes")


class GarminOAuthProvider:
    def __init__(self, settings: AuthSettings, signer: TokenSigner, clients: ClientStore) -> None:
        self._settings = settings
        self._signer = signer
        self._clients = clients
        self._subject = str(settings.github_allowed_user_id)
        # Codes already exchanged, kept for their 60-second lifetime so a stolen
        # code cannot be used twice. Memory is fine here: the container cannot
        # scale to zero in the seconds between issuing a code and its exchange,
        # and PKCE already protects the code if it ever did.
        self._used_codes: dict[str, float] = {}

    # --- clients -----------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients.register(client_info)

    # --- /authorize --------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Return the URL the browser is sent to: GitHub's login page."""
        if params.resource is not None and not self._is_this_server(params.resource):
            # RFC 8707: the client is asking for a token for some other server.
            raise AuthorizeError("invalid_target", "This server only issues tokens for itself.")
        login_token = self._signer.sign(
            "login",
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_explicit": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "state": params.state,
                "scopes": params.scopes or [],
            },
            LOGIN_TTL_SECONDS,
        )
        query = urlencode(
            {
                "client_id": self._settings.github_client_id,
                "redirect_uri": f"{self._settings.public_url}/auth/github/callback",
                "state": login_token,
                "allow_signup": "false",
                # No `scope`: we only need to know WHO logged in (the public user id),
                # so we ask GitHub for no permissions at all.
            }
        )
        return f"{self._settings.github_oauth_url}/authorize?{query}"

    def _is_this_server(self, resource: str) -> bool:
        ours = (self._settings.resource_url, self._settings.public_url)
        return resource.rstrip("/").lower() in {url.lower() for url in ours}

    def issue_authorization_code(self, grant: dict[str, Any]) -> str:
        """Called by the consent screen once the owner has approved the client."""
        claims = {field: grant[field] for field in GRANT_FIELDS}
        return self._signer.sign("code", {**claims, "sub": self._subject}, CODE_TTL_SECONDS)

    # --- /token: authorization code ----------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        try:
            claims = self._signer.verify("code", authorization_code)
        except InvalidToken:
            return None
        if claims["jti"] in self._used_codes or claims.get("sub") != self._subject:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=claims["scopes"],
            expires_at=claims["exp"],
            client_id=claims["client_id"],
            code_challenge=claims["code_challenge"],
            redirect_uri=claims["redirect_uri"],
            redirect_uri_provided_explicitly=claims["redirect_uri_explicit"],
            resource=self._settings.resource_url,
            subject=claims["sub"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # The SDK has already checked the client, the redirect URI and PKCE.
        claims = self._signer.verify("code", authorization_code.code)
        now = time.time()
        self._used_codes = {jti: exp for jti, exp in self._used_codes.items() if exp > now}
        if claims["jti"] in self._used_codes:
            raise TokenError("invalid_grant", "authorization code already used")
        self._used_codes[claims["jti"]] = claims["exp"]
        return self._issue_tokens(authorization_code.client_id, authorization_code.scopes)

    # --- /token: refresh token ---------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        try:
            claims = self._signer.verify("refresh", refresh_token)
        except InvalidToken:
            return None
        if claims.get("sub") != self._subject:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=claims["client_id"],
            scopes=claims["scopes"],
            expires_at=claims["exp"],
            resource=self._settings.resource_url,
            subject=claims["sub"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        return self._issue_tokens(refresh_token.client_id, scopes)

    def _issue_tokens(self, client_id: str, scopes: list[str]) -> OAuthToken:
        claims = {"client_id": client_id, "scopes": scopes, "sub": self._subject}
        return OAuthToken(
            # `aud` binds the access token to this server: a token issued for another
            # server (or by this one for another purpose) is rejected.
            access_token=self._signer.sign(
                "access", {**claims, "aud": self._settings.resource_url}, ACCESS_TOKEN_TTL_SECONDS
            ),
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=self._signer.sign("refresh", claims, REFRESH_TOKEN_TTL_SECONDS),
            scope=" ".join(scopes) if scopes else None,
        )

    # --- every MCP request -------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            claims = self._signer.verify("access", token, audience=self._settings.resource_url)
        except InvalidToken:
            return None
        # Checked on every request, so changing GITHUB_ALLOWED_USER_ID locks out
        # tokens issued to the previous account.
        if claims.get("sub") != self._subject:
            return None
        return AccessToken(
            token=token,
            client_id=claims["client_id"],
            scopes=claims["scopes"],
            expires_at=claims["exp"],
            resource=self._settings.resource_url,
            subject=claims["sub"],
        )

    # --- not supported -----------------------------------------------------

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Self-contained tokens cannot be revoked one by one, so the revocation
        # endpoint is not advertised. To revoke everything, rotate the signing key.
        return None

    async def exchange_identity_assertion(self, client: OAuthClientInformationFull, params: Any) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "The JWT bearer grant is not supported.")
