"""Tests for the Garmin layer using fakes: no network and no real key."""

import asyncio
from types import SimpleNamespace

import pytest
from garmin_personal_api import HttpError

from garmin_mcp.garmin import GarminError, GarminService


class FakeAuthenticator:
    def __init__(self):
        self.calls = []

    def obtain_token(self, api_key, *, force_refresh=False):
        self.calls.append(force_refresh)
        return SimpleNamespace(access_token=f"token-{len(self.calls)}", expires_at=0)


class FakeClient:
    """Mimics the official client; `fail_with` simulates Garmin error responses."""

    def __init__(self, token):
        self.token = token
        self.requests = 0
        self.fail_with: list[int] = []

    def update_bearer_token(self, token):
        self.token = token

    def get_daily_sleep(self, day):
        self.requests += 1
        if self.fail_with:
            raise HttpError(
                status_code=self.fail_with.pop(0),
                method="GET",
                url="https://personal.connectapi.garmin.com/sleep-service/sleep/dailySleep?date=x",
                response_text='{"private_value": 42}',
            )
        return {"calendarDate": day, "token": self.token}


def make_service(**kwargs):
    auth = FakeAuthenticator()
    clients = []

    def factory(token):
        clients.append(FakeClient(token))
        return clients[-1]

    service = GarminService("fake-key", authenticator=auth, client_factory=factory, **kwargs)
    return service, auth, clients


def call(service, *args):
    return asyncio.run(service.call(*args))


def test_caches_identical_calls():
    service, _, clients = make_service()
    first = call(service, "get_daily_sleep", "2026-11-01")
    second = call(service, "get_daily_sleep", "2026-11-01")
    assert first == second
    assert clients[0].requests == 1


def test_cache_expires():
    now = [0.0]
    service, _, clients = make_service(cache_ttl_seconds=10, clock=lambda: now[0])
    call(service, "get_daily_sleep", "2026-11-01")
    now[0] = 11
    call(service, "get_daily_sleep", "2026-11-01")
    assert clients[0].requests == 2


def test_cache_can_be_disabled():
    service, _, clients = make_service(cache_ttl_seconds=0)
    call(service, "get_daily_sleep", "2026-11-01")
    call(service, "get_daily_sleep", "2026-11-01")
    assert clients[0].requests == 2


def test_401_refreshes_token_and_retries_once():
    service, auth, clients = make_service()
    call(service, "get_daily_sleep", "2026-11-01")  # creates the client
    clients[0].fail_with = [401]
    result = call(service, "get_daily_sleep", "2026-11-02")
    assert auth.calls[-1] is True  # a new token was forced
    assert result["token"] == clients[0].token != "token-1"


def test_second_401_reports_key_problem_without_response_body():
    service, _, clients = make_service()
    call(service, "get_daily_sleep", "2026-11-01")
    clients[0].fail_with = [401, 401]
    with pytest.raises(GarminError) as excinfo:
        call(service, "get_daily_sleep", "2026-11-02")
    message = str(excinfo.value)
    assert "401" in message and "key" in message
    assert "private_value" not in message  # the response body is never propagated
    assert excinfo.value.__cause__ is None  # not even as a chained exception


def test_other_http_errors_are_not_retried():
    service, _, clients = make_service()
    call(service, "get_daily_sleep", "2026-11-01")
    clients[0].fail_with = [500]
    with pytest.raises(GarminError, match="HTTP 500"):
        call(service, "get_daily_sleep", "2026-11-02")
    assert clients[0].requests == 2
