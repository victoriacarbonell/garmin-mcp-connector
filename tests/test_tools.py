"""Tests for the tools using a fake service (no network and no key)."""

import asyncio

import pytest

from garmin_mcp import tools
from garmin_mcp.garmin import GarminError


class FakeService:
    def __init__(self, responses=None, failing=()):
        self.responses = responses or {}
        self.failing = set(failing)
        self.calls = []

    async def call(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        if method in self.failing:
            raise GarminError(f"simulated failure in {method}")
        return self.responses.get(method)


def run(coro):
    return asyncio.run(coro)


def test_list_activities_trims_fields_and_passes_filters():
    service = FakeService({
        "search_activities": [{
            "activityId": 1, "activityName": "Easy run", "distance": 5000.0,
            "ownerFullName": "Must not appear", "maxDepth": None, "comments": [],
            "activityType": {"typeKey": "running"},
        }]
    })
    result = run(tools.list_activities(service, "2026-11-01", "2026-11-07", "running", limit=5))

    assert result["source"] == tools.SOURCE
    assert result["own_calculations"] is None
    assert result["garmin_data"]["activities"] == [{
        "activityId": 1, "activityName": "Easy run", "distance": 5000.0,
        "activityType": {"typeKey": "running"},
    }]
    assert service.calls[0][2] == {
        "start": 0, "limit": 5, "start_date": "2026-11-01", "end_date": "2026-11-07",
        "activity_type": "running",
    }


def test_list_activities_warns_when_page_is_full():
    service = FakeService({"search_activities": [{"activityId": i} for i in range(2)]})
    result = run(tools.list_activities(service, "2026-11-01", "2026-11-07", limit=2))
    assert "offset=2" in result["warnings"][0]


@pytest.mark.parametrize(
    "start,end", [("01/11/2026", "2026-11-07"), ("2026-11-07", "2026-11-01")]
)
def test_invalid_dates_are_rejected_before_calling_garmin(start, end):
    service = FakeService()
    with pytest.raises(ValueError):
        run(tools.list_activities(service, start, end))
    assert service.calls == []


def test_get_activity_optionally_includes_workouts():
    service = FakeService({"get_activity": {"activityId": 7}, "get_activity_workouts": [{"w": 1}]})
    assert set(run(tools.get_activity(service, 7))["garmin_data"]) == {"activity"}
    with_workouts = run(tools.get_activity(service, 7, include_workouts=True))
    assert set(with_workouts["garmin_data"]) == {"activity", "workouts"}


def test_daily_health_survives_a_failing_section_and_hides_hrv_readings():
    service = FakeService(
        {
            "get_daily_summaries": {"data": [{"userProfilePk": 99, "heartRate": {"restingValue": 52}}]},
            "get_daily_hrv": {"hrvSummary": {"lastNightAvg": 60}, "hrvReadings": [{"hrvValue": 58}]},
        },
        failing={"get_daily_sleep"},
    )
    result = run(tools.get_daily_health(service, "2026-11-10"))
    data = result["garmin_data"]

    assert data["daily_summary"] == {"data": [{"heartRate": {"restingValue": 52}}]}  # no account ID
    assert data["hrv"] == {"hrvSummary": {"lastNightAvg": 60}}
    assert "sleep" not in data
    assert any(w.startswith("sleep:") for w in result["warnings"])

    full = run(tools.get_daily_health(service, "2026-11-10", include_hrv_readings=True))
    assert full["garmin_data"]["hrv"]["hrvReadings"] == [{"hrvValue": 58}]


def test_daily_health_fails_if_every_section_fails():
    service = FakeService(failing={"get_daily_summaries", "get_daily_sleep", "get_daily_hrv"})
    with pytest.raises(GarminError):
        run(tools.get_daily_health(service, "2026-11-10"))


def test_health_range_enforces_documented_limits():
    service = FakeService()
    with pytest.raises(ValueError, match="31 days"):
        run(tools.get_health_range(service, "2026-01-01", "2026-03-01", ["sleep"]))
    assert service.calls == []
    # The daily summary does allow long ranges (up to 1 year).
    run(tools.get_health_range(service, "2026-01-01", "2026-03-01", ["daily_summary"]))
    assert service.calls[0][0] == "get_daily_summaries"


def test_health_range_rejects_unknown_metrics():
    with pytest.raises(ValueError, match="Invalid metrics"):
        run(tools.get_health_range(FakeService(), "2026-11-01", "2026-11-07", ["steps"]))
