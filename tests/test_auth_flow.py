"""End-to-end tests of the authorization flow against the real application.

The app runs in-process (no network). The only thing replaced is the call to
GitHub, so each test can decide which GitHub account "logged in".
"""

import asyncio
import base64
import hashlib
import re
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest

from garmin_mcp.app import build_app
from garmin_mcp.auth import github
from garmin_mcp.config import AuthSettings, Settings

BASE = "http://localhost:8000"
OWNER_ID = 4242
REDIRECT_URI = "http://localhost:9999/callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
}  # fmt: skip


def make_settings(signing_key: str = "k" * 40) -> Settings:
    auth = AuthSettings(
        public_url=BASE,
        signing_key=signing_key,
        github_client_id="gh-client",
        github_client_secret="gh-secret",
        github_allowed_user_id=OWNER_ID,
    )
    return Settings(garmin_api_key="unused", auth=auth)


def run(scenario, *, github_user_id=OWNER_ID, monkeypatch):
    """Run `scenario(http)` against a freshly built app with GitHub faked."""

    async def fake_github_user_id(settings, code):
        return github_user_id

    monkeypatch.setattr(github, "_github_user_id", fake_github_user_id)

    async def main():
        app = build_app(make_settings())
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url=BASE) as http:
                return await scenario(http)

    return asyncio.run(main())


async def register(http, **extra):
    response = await http.post(
        "/register",
        json={"client_name": "Test client", "redirect_uris": [REDIRECT_URI], "token_endpoint_auth_method": "none", **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def login(http, client_id, *, action="allow"):
    """Drive /authorize -> GitHub -> consent screen and return the final redirect's query."""
    response = await http.get(
        "/authorize",
        params={
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI,
            "code_challenge": CHALLENGE, "code_challenge_method": "S256", "state": "client-state",
            "resource": f"{BASE}/mcp",
        },
    )  # fmt: skip
    assert response.status_code == 302, response.text
    to_github = urlparse(response.headers["location"])
    assert to_github.netloc == "github.com"
    login_state = parse_qs(to_github.query)["state"][0]

    page = await http.get("/auth/github/callback", params={"code": "gh-code", "state": login_state})
    if page.status_code != 200:
        return page
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "localhost:9999" in page.text  # the owner sees where access would be sent
    consent_token = re.search(r'name="consent" value="([^"]+)"', page.text).group(1)

    done = await http.post("/auth/consent", data={"consent": consent_token, "action": action})
    assert done.status_code == 303
    assert done.headers["location"].startswith(REDIRECT_URI)
    return {key: values[0] for key, values in parse_qs(urlparse(done.headers["location"]).query).items()}


async def exchange(http, client_id, code, verifier=VERIFIER, **extra):
    return await http.post(
        "/token",
        data={
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "redirect_uri": REDIRECT_URI, "code_verifier": verifier, **extra,
        },
    )  # fmt: skip


def test_unauthenticated_request_points_to_the_metadata(monkeypatch):
    async def scenario(http):
        response = await http.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        assert response.status_code == 401
        challenge = response.headers["www-authenticate"]
        assert "/.well-known/oauth-protected-resource/mcp" in challenge

        resource = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
        server = (await http.get("/.well-known/oauth-authorization-server")).json()
        assert resource["resource"] == f"{BASE}/mcp"
        assert resource["authorization_servers"] == [server["issuer"]]  # exact string match
        assert server["code_challenge_methods_supported"] == ["S256"]
        assert server["client_id_metadata_document_supported"] is True
        assert server["authorization_response_iss_parameter_supported"] is True
        assert "none" in server["token_endpoint_auth_methods_supported"]
        assert server["registration_endpoint"] == f"{BASE}/register"

    run(scenario, monkeypatch=monkeypatch)


def test_full_flow_gives_a_token_that_opens_the_mcp_endpoint(monkeypatch):
    async def scenario(http):
        client = await register(http)
        result = await login(http, client["client_id"])
        assert result["state"] == "client-state"
        assert result["iss"] == BASE

        tokens = (await exchange(http, client["client_id"], result["code"])).json()
        assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600

        ok = await http.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {tokens['access_token']}"}
        )
        assert ok.status_code == 200, ok.text

        # A refresh token is not an access token, even though the same key signs both.
        wrong_type = await http.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {tokens['refresh_token']}"}
        )
        assert wrong_type.status_code == 401

        refreshed = await http.post(
            "/token",
            data={"grant_type": "refresh_token", "client_id": client["client_id"], "refresh_token": tokens["refresh_token"]},
        )
        assert refreshed.status_code == 200 and refreshed.json()["access_token"]

    run(scenario, monkeypatch=monkeypatch)


def test_another_github_account_is_refused(monkeypatch):
    async def scenario(http):
        client = await register(http)
        page = await login(http, client["client_id"])
        assert page.status_code == 403
        assert "consent" not in page.text  # no consent form, so no way forward

    run(scenario, github_user_id=OWNER_ID + 1, monkeypatch=monkeypatch)


def test_denying_consent_returns_an_error_and_no_code(monkeypatch):
    async def scenario(http):
        client = await register(http)
        result = await login(http, client["client_id"], action="deny")
        assert result["error"] == "access_denied" and "code" not in result

    run(scenario, monkeypatch=monkeypatch)


def test_code_is_single_use_and_bound_to_pkce(monkeypatch):
    async def scenario(http):
        client = await register(http)

        result = await login(http, client["client_id"])
        wrong_verifier = await exchange(http, client["client_id"], result["code"], verifier="w" * 64)
        assert wrong_verifier.json()["error"] == "invalid_grant"

        result = await login(http, client["client_id"])
        assert (await exchange(http, client["client_id"], result["code"])).status_code == 200
        replay = await exchange(http, client["client_id"], result["code"])
        assert replay.json()["error"] == "invalid_grant"

    run(scenario, monkeypatch=monkeypatch)


def test_confidential_client_needs_its_secret(monkeypatch):
    async def scenario(http):
        client = await register(http, token_endpoint_auth_method="client_secret_post")
        assert client["client_secret"]
        result = await login(http, client["client_id"])
        without_secret = await exchange(http, client["client_id"], result["code"])
        assert without_secret.status_code == 401
        with_secret = await exchange(http, client["client_id"], result["code"], client_secret=client["client_secret"])
        assert with_secret.status_code == 200

    run(scenario, monkeypatch=monkeypatch)


def test_forged_and_foreign_tokens_are_rejected(monkeypatch):
    async def scenario(http):
        client = await register(http)
        result = await login(http, client["client_id"])
        token = (await exchange(http, client["client_id"], result["code"])).json()["access_token"]
        header, payload, signature = token.split(".")
        candidates = [
            "not-a-token",
            f"{header}.{payload}.{signature[:-2]}AA",  # altered signature
            f"{header}.{payload}.",  # signature removed
        ]
        for candidate in candidates:
            response = await http.post(
                "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {candidate}"}
            )
            assert response.status_code == 401, candidate

        # An unregistered client_id (not signed by us) cannot even start a login.
        unknown = await http.get(
            "/authorize",
            params={"response_type": "code", "client_id": "made-up", "redirect_uri": REDIRECT_URI,
                    "code_challenge": CHALLENGE, "code_challenge_method": "S256"},
        )  # fmt: skip
        assert unknown.status_code == 400

    run(scenario, monkeypatch=monkeypatch)


def test_redirect_uri_must_match_the_registration(monkeypatch):
    async def scenario(http):
        client = await register(http)
        response = await http.get(
            "/authorize",
            params={"response_type": "code", "client_id": client["client_id"],
                    "redirect_uri": "https://attacker.example/steal",
                    "code_challenge": CHALLENGE, "code_challenge_method": "S256"},
        )  # fmt: skip
        assert response.status_code == 400  # shown to the user, never redirected

    run(scenario, monkeypatch=monkeypatch)


def test_token_from_a_different_signing_key_is_rejected(monkeypatch):
    """Rotating the signing key must log every client out."""
    from garmin_mcp.auth.tokens import TokenSigner

    foreign = TokenSigner("z" * 40, BASE).sign(
        "access", {"client_id": "x", "scopes": [], "sub": str(OWNER_ID), "aud": f"{BASE}/mcp"}, 3600
    )

    async def scenario(http):
        response = await http.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Authorization": f"Bearer {foreign}"}
        )
        assert response.status_code == 401

    run(scenario, monkeypatch=monkeypatch)


@pytest.mark.parametrize("resource", ["https://other-server.example/mcp"])
def test_token_for_another_resource_is_not_issued(resource, monkeypatch):
    async def scenario(http):
        client = await register(http)
        response = await http.get(
            "/authorize",
            params={"response_type": "code", "client_id": client["client_id"], "redirect_uri": REDIRECT_URI,
                    "code_challenge": CHALLENGE, "code_challenge_method": "S256", "resource": resource},
        )  # fmt: skip
        assert response.status_code == 302
        assert "error=invalid_target" in response.headers["location"]
        assert response.headers["location"].startswith(REDIRECT_URI)

    run(scenario, monkeypatch=monkeypatch)
