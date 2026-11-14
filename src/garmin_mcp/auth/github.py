"""The pages the owner's browser visits while authorizing a client.

    client -> /authorize -> GitHub login -> /auth/github/callback
           -> consent screen -> POST /auth/consent -> back to the client with a code

Two separate checks happen here, and both are needed:

1. WHO is at the keyboard. GitHub tells us, and only GITHUB_ALLOWED_USER_ID
   passes. We compare the numeric id, which never changes, not the username,
   which can be renamed and then taken by someone else.

2. WHICH client gets access. Anyone can register a client and send the owner a
   link. Because the owner is usually already logged in to GitHub, step 1 would
   pass silently and the attacker's client would receive a token. The consent
   screen stops that: it shows where the token would be sent and waits for an
   explicit "Allow". (The MCP spec calls this the confused deputy problem.)
"""

from __future__ import annotations

import html
import logging
from typing import Any
from urllib.parse import urlparse

import httpx2
from mcp.server.auth.provider import construct_redirect_uri
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from ..config import AuthSettings
from .clients import ClientStore
from .provider import CONSENT_TTL_SECONDS, GRANT_FIELDS, GarminOAuthProvider
from .tokens import InvalidToken, TokenSigner

logger = logging.getLogger("garmin_mcp.auth")

GITHUB_TIMEOUT_SECONDS = 10.0

# The consent page must never be embedded in another site (clickjacking: a
# hidden frame positioned so that an innocent click lands on "Allow").
_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;line-height:1.5;color:#1a1a1a}}
code{{background:#f0f0f0;padding:.1rem .3rem;border-radius:.25rem;word-break:break-all}}
.box{{border:1px solid #ccc;border-radius:.5rem;padding:1rem;margin:1rem 0}}
button{{font-size:1rem;padding:.6rem 1.2rem;border-radius:.4rem;border:1px solid #888;cursor:pointer;margin-right:.5rem}}
.allow{{background:#1a7f37;color:#fff;border-color:#1a7f37}}
.muted{{color:#555;font-size:.9rem}}
</style></head><body>{body}</body></html>"""


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=html.escape(title), body=body), status_code, headers=_PAGE_HEADERS)


def _error_page(message: str, status_code: int) -> HTMLResponse:
    return _page("Authorization failed", f"<h1>Authorization failed</h1><p>{html.escape(message)}</p>", status_code)


def _consent_page(client_name: str | None, grant: dict[str, Any], consent_token: str) -> HTMLResponse:
    redirect_uri = grant["redirect_uri"]
    redirect_host = urlparse(redirect_uri).netloc or redirect_uri
    client_id = grant["client_id"]
    # Everything the client told us about itself is untrusted text: escape it all.
    identity = (
        f"<p>Client metadata URL: <code>{html.escape(client_id)}</code></p>"
        if client_id.startswith("https://")
        else ""
    )
    body = f"""
<h1>Allow access to your Garmin data?</h1>
<div class="box">
<p>The application <strong>{html.escape(client_name or "(no name given)")}</strong> wants to read
your Garmin activity and health data through this connector.</p>
<p>If you allow it, access will be sent to: <strong>{html.escape(redirect_host)}</strong></p>
<p class="muted">Full address: <code>{html.escape(redirect_uri)}</code></p>
{identity}
</div>
<p class="muted">The name above is chosen by the application and proves nothing. Only allow this if
you started this connection yourself, just now, and recognise the address.</p>
<form method="post" action="/auth/consent">
<input type="hidden" name="consent" value="{html.escape(consent_token)}">
<button class="allow" type="submit" name="action" value="allow">Allow</button>
<button type="submit" name="action" value="deny">Deny</button>
</form>"""
    return _page("Allow access?", body)


async def _github_user_id(settings: AuthSettings, code: str) -> int | None:
    """Exchange GitHub's code for the id of the account that logged in."""
    try:
        async with httpx2.AsyncClient(timeout=GITHUB_TIMEOUT_SECONDS) as http:
            token_response = await http.post(
                f"{settings.github_oauth_url}/access_token",
                headers={"Accept": "application/json"},
                data={
                    "client_id": settings.github_client_id,
                    "client_secret": settings.github_client_secret,
                    "code": code,
                    "redirect_uri": f"{settings.public_url}/auth/github/callback",
                },
            )
            github_token = token_response.json().get("access_token")
            if not github_token:
                return None
            user_response = await http.get(
                f"{settings.github_api_url}/user",
                headers={"Authorization": f"Bearer {github_token}", "Accept": "application/vnd.github+json"},
            )
            if user_response.status_code != 200:
                return None
            # The GitHub token is used for this single call and then discarded.
            return int(user_response.json()["id"])
    except (httpx2.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return None


def build_login_routes(
    settings: AuthSettings,
    provider: GarminOAuthProvider,
    signer: TokenSigner,
    clients: ClientStore,
    issuer: str,
) -> list[Route]:
    def back_to_client(grant: dict[str, Any], **params: str | None) -> RedirectResponse:
        # RFC 9207: `iss` lets the client check which server answered.
        url = construct_redirect_uri(grant["redirect_uri"], state=grant.get("state"), iss=issuer, **params)
        # 303 turns the consent POST into a GET on the client's redirect URI.
        return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})

    async def github_callback(request: Request) -> Response:
        try:
            grant = signer.verify("login", request.query_params.get("state", ""))
        except InvalidToken:
            return _error_page("This login link is invalid or has expired. Start again from your application.", 400)

        code = request.query_params.get("code")
        if request.query_params.get("error") or not code:
            return back_to_client(grant, error="access_denied", error_description="GitHub login was cancelled")

        user_id = await _github_user_id(settings, code)
        if user_id is None:
            return _error_page("Could not verify your identity with GitHub. Try again.", 502)
        if user_id != settings.github_allowed_user_id:
            logger.warning("login refused: GitHub account is not the owner")
            return _error_page("This GitHub account is not allowed to use this connector.", 403)

        client = await clients.get(grant["client_id"])
        if client is None:
            return _error_page("Unknown application. Start again from your application.", 400)
        consent_token = signer.sign(
            "consent", {field: grant.get(field) for field in GRANT_FIELDS}, CONSENT_TTL_SECONDS
        )
        return _consent_page(client.client_name, grant, consent_token)

    async def consent(request: Request) -> Response:
        form = await request.form()
        try:
            grant = signer.verify("consent", str(form.get("consent", "")))
        except InvalidToken:
            return _error_page("This consent form has expired. Start again from your application.", 400)
        if form.get("action") != "allow":
            return back_to_client(grant, error="access_denied", error_description="The owner denied the request")
        logger.info("client authorized by the owner")
        return back_to_client(grant, code=provider.issue_authorization_code(grant))

    return [
        Route("/auth/github/callback", github_callback, methods=["GET"]),
        Route("/auth/consent", consent, methods=["POST"]),
    ]
