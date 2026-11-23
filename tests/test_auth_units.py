"""Unit tests for the pieces of the authorization server that need no HTTP."""

import pytest
from mcp.shared.auth import InvalidRedirectUriError
from pydantic import AnyUrl

from garmin_mcp import config
from garmin_mcp.auth.clients import _client_from_document, _is_valid_metadata_url
from garmin_mcp.auth.tokens import InvalidToken, TokenSigner

URL = "https://client.example/oauth/metadata.json"
DOCUMENT = {"client_id": URL, "client_name": "Example", "redirect_uris": ["https://client.example/callback"]}


def test_token_types_are_not_interchangeable():
    signer = TokenSigner("k" * 40, "https://issuer.example")
    token = signer.sign("refresh", {"sub": "1"}, 60)
    assert signer.verify("refresh", token)["sub"] == "1"
    with pytest.raises(InvalidToken):
        signer.verify("access", token)


def test_expired_and_foreign_issuer_tokens_are_rejected():
    signer = TokenSigner("k" * 40, "https://issuer.example")
    with pytest.raises(InvalidToken):
        signer.verify("access", signer.sign("access", {}, -1))
    other_issuer = TokenSigner("k" * 40, "https://elsewhere.example").sign("access", {}, 60)
    with pytest.raises(InvalidToken):
        signer.verify("access", other_issuer)


def test_unsigned_token_is_rejected():
    import jwt

    signer = TokenSigner("k" * 40, "https://issuer.example")
    unsigned = jwt.encode({"typ": "access", "iss": "https://issuer.example", "iat": 0, "jti": "x"}, "", algorithm="none")
    with pytest.raises(InvalidToken):
        signer.verify("access", unsigned)


@pytest.mark.parametrize(
    "url,valid",
    [
        (URL, True),
        ("http://client.example/metadata.json", False),  # must be https
        ("https://client.example", False),  # needs a path
        ("https://client.example/", False),
        ("https://user:pw@client.example/metadata.json", False),
        ("https://client.example/metadata.json#fragment", False),
    ],
)
def test_metadata_url_rules(url, valid):
    assert _is_valid_metadata_url(url) is valid


def test_metadata_document_is_accepted_as_a_public_client():
    client = _client_from_document(URL, DOCUMENT)
    assert client.client_id == URL
    assert client.token_endpoint_auth_method == "none" and client.client_secret is None


@pytest.mark.parametrize(
    "document",
    [
        {**DOCUMENT, "client_id": "https://someone-else.example/metadata.json"},  # claims another URL
        {**DOCUMENT, "redirect_uris": []},
        {**DOCUMENT, "client_name": ""},
        {**DOCUMENT, "token_endpoint_auth_method": "client_secret_post"},
        ["not", "an", "object"],
    ],
)
def test_bad_metadata_documents_are_rejected(document):
    assert _client_from_document(URL, document) is None


# Claude Code's real client metadata document registers loopback URIs with no port
# and then listens on a random one (RFC 8252 section 7.3).
LOOPBACK_CLIENT = _client_from_document(
    URL, {**DOCUMENT, "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"]}
)


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://localhost:43491/callback",
        "http://127.0.0.1:8765/callback",
        "http://localhost/callback",  # exact match still works
    ],
)
def test_loopback_redirect_accepts_any_port(redirect_uri):
    assert str(LOOPBACK_CLIENT.validate_redirect_uri(AnyUrl(redirect_uri))) == redirect_uri


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "http://localhost:43491/other",  # different path
        "http://localhost:43491/callback?x=1",  # extra query
        "https://localhost:43491/callback",  # different scheme
        "http://[::1]:43491/callback",  # loopback, but not a registered host
        "http://localhost.evil.example:43491/callback",
        "http://evil.example:43491/callback",
        "http://user@localhost:43491/callback",
    ],
)
def test_loopback_redirect_rejects_anything_else(redirect_uri):
    with pytest.raises(InvalidRedirectUriError):
        LOOPBACK_CLIENT.validate_redirect_uri(AnyUrl(redirect_uri))


def test_non_loopback_redirect_still_needs_an_exact_match():
    client = _client_from_document(URL, DOCUMENT)  # https://client.example/callback
    assert client.validate_redirect_uri(AnyUrl("https://client.example/callback"))
    with pytest.raises(InvalidRedirectUriError):
        client.validate_redirect_uri(AnyUrl("https://client.example:8443/callback"))


@pytest.fixture
def env(monkeypatch):
    for name in list(config.os.environ):
        if name.startswith(("GARMIN_", "GITHUB_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", "key")
    return monkeypatch


def test_refuses_to_listen_publicly_without_authorization(env):
    env.setenv("GARMIN_MCP_AUTH_DISABLED", "1")
    assert config.load_settings().auth is None  # fine on loopback
    env.setenv("GARMIN_MCP_HOST", "0.0.0.0")
    with pytest.raises(config.ConfigError, match="non-loopback"):
        config.load_settings()


def test_authorization_is_required_by_default(env):
    with pytest.raises(config.ConfigError, match="GARMIN_MCP_SIGNING_KEY"):
        config.load_settings()


def test_public_url_must_be_https_and_key_long_enough(env):
    env.setenv("GITHUB_CLIENT_ID", "a")
    env.setenv("GITHUB_CLIENT_SECRET", "b")
    env.setenv("GITHUB_ALLOWED_USER_ID", "1")
    env.setenv("GARMIN_MCP_SIGNING_KEY", "k" * 40)
    env.setenv("GARMIN_MCP_PUBLIC_URL", "http://example.com")
    with pytest.raises(config.ConfigError, match="https"):
        config.load_settings()
    env.setenv("GARMIN_MCP_PUBLIC_URL", "https://example.com")
    assert config.load_settings().auth.resource_url == "https://example.com/mcp"
    env.setenv("GARMIN_MCP_SIGNING_KEY", "short")
    with pytest.raises(config.ConfigError, match="at least"):
        config.load_settings()
