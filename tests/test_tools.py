"""Tests for the tools using a fake service (no network and no key)."""

import asyncio
import logging

import pytest

from garmin_mcp import tools
from garmin_mcp.garmin import GarminError


class FakeService:
    """`failing`: methods that fail with no HTTP status (e.g. network error).
    `errors`: method -> GarminError to raise, to simulate a specific HTTP status."""

    def __init__(self, responses=None, failing=(), errors=None):
        self.responses = responses or {}
        self.failing = set(failing)
        self.errors = errors or {}
        self.calls = []

    async def call(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        if method in self.errors:
            raise self.errors[method]
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


def test_get_activity_drops_user_info_from_metadata():
    service = FakeService({"get_activity": {
        "activityId": 7,
        "metadataDTO": {
            "lapCount": 7,
            "userInfoDto": {"fullname": "Must not appear", "profileImageUrlSmall": "https://x"},
        },
    }})
    activity = run(tools.get_activity(service, 7))["garmin_data"]["activity"]
    assert activity["metadataDTO"] == {"lapCount": 7}


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


# Same shape as real Personal API responses. Every value is invented: never paste
# real health data into tests, the repository is public.
RANGE_DAILY_SUMMARIES = {"data": [{
    "calendarDate": "2026-01-15",
    "uuid": "abc",
    "movement": {
        "steps": {"goal": 10000, "value": 12000, "distanceInMeters": 9000.0},
        "pushes": {},
        "floorsAscended": {"goal": 10, "value": 20, "distanceInMeters": 60.0},
        "floorsDescended": {"value": 20},
    },
    "calories": {"burnedResting": 1500, "burnedActive": 500, "burnedTotal": 2000},
    "heartRate": {"minValue": 50, "maxValue": 150, "restingValue": 60},
    "stress": {"avgLevel": 25, "maxLevel": 90, "qualifier": "balanced", "restProportion": 0.5},
    "bodyBattery": {
        "minValue": 20, "maxValue": 90, "chargedValue": 60, "drainedValue": 70,
        "dynamicFeedbackEvent": {"feedbackShortType": "SLEEP_PREPARATION"},
    },
    "intensityMinutes": {"goal": 150, "moderate": 30, "vigorous": 10},
    "pulseOx": {"avgValue": 95, "minValue": 90},
    "respiration": {"avgValue": 15, "maxValue": 20},
    "hydration": {"goalInMl": 2000},
    "wellnessChronology": {"totalDurationInMillis": 86400000},
}]}
RANGE_SLEEP = [{
    "calendarDate": "2026-01-15",
    "sleepTimeSeconds": 28800, "deepSleepSeconds": 3600, "lightSleepSeconds": 18000,
    "remSleepSeconds": 7200, "awakeSleepSeconds": 600, "awakeCount": 1,
    "avgHeartRate": 60.0, "averageSpO2Value": 95.0, "averageRespirationValue": 15.0,
    "avgSleepStress": 20.0,
    "sleepStartTimestampGMT": 1768440000000,
    "sleepScores": {
        "overall": {"value": 80, "qualifierKey": "GOOD"},
        "remPercentage": {"value": 25, "optimalStart": 21.0},
    },
    "sleepNeed": {"baseline": 480},
}]
RANGE_HRV = {"hrvSummaries": [{"calendarDate": "2026-01-15", "lastNightAvg": 60, "status": "BALANCED"}]}


def test_health_range_keeps_only_trend_fields():
    service = FakeService({
        "get_daily_summaries": RANGE_DAILY_SUMMARIES,
        "get_daily_sleep_summaries": RANGE_SLEEP,
        "get_daily_hrv_summaries": RANGE_HRV,
    })
    data = run(tools.get_health_range(service, "2026-01-15", "2026-01-15"))["garmin_data"]

    assert data["daily_summary"] == {"data": [{
        "calendarDate": "2026-01-15",
        "movement": {
            "steps": {"goal": 10000, "value": 12000, "distanceInMeters": 9000.0},
            "floorsAscended": {"value": 20},
        },
        "calories": {"burnedActive": 500, "burnedTotal": 2000},
        "heartRate": {"minValue": 50, "maxValue": 150, "restingValue": 60},
        "stress": {"avgLevel": 25, "maxLevel": 90, "qualifier": "balanced"},
        "bodyBattery": {"minValue": 20, "maxValue": 90, "chargedValue": 60, "drainedValue": 70},
        "intensityMinutes": {"moderate": 30, "vigorous": 10},
        "pulseOx": {"avgValue": 95},
        "respiration": {"avgValue": 15},
    }]}
    assert data["sleep"] == [{
        "calendarDate": "2026-01-15",
        "sleepTimeSeconds": 28800, "deepSleepSeconds": 3600, "lightSleepSeconds": 18000,
        "remSleepSeconds": 7200, "awakeSleepSeconds": 600, "awakeCount": 1,
        "avgHeartRate": 60.0, "averageSpO2Value": 95.0, "averageRespirationValue": 15.0,
        "avgSleepStress": 20.0,
        "sleepScores": {"overall": {"value": 80, "qualifierKey": "GOOD"}},
    }]
    # HRV is already small and comes back untouched.
    assert data["hrv"] == RANGE_HRV


def test_health_range_skips_fields_garmin_did_not_send():
    service = FakeService({"get_daily_sleep_summaries": [{"calendarDate": "2026-01-15", "awakeCount": 2}]})
    data = run(tools.get_health_range(service, "2026-01-15", "2026-01-15", ["sleep"]))["garmin_data"]
    assert data["sleep"] == [{"calendarDate": "2026-01-15", "awakeCount": 2}]


def test_health_range_rejects_unknown_metrics():
    with pytest.raises(ValueError, match="Invalid metrics"):
        run(tools.get_health_range(FakeService(), "2026-11-01", "2026-11-07", ["steps"]))


# --- get_menstrual_cycle ------------------------------------------------------

CYCLE_SUMMARY = {"dayInCycle": 12, "currentPhase": 2, "predictedCycleLength": 28, "userProfilePk": 99}
CYCLE_DAY_VIEW = {"dayLog": {"flow": "LIGHT", "userProfilePk": 99}, "daySummary": {"dayInCycle": 12}}


def http_error(status, path="/periodichealth-service/menstrualcycle/summary/2026-11-10"):
    return GarminError(f"Garmin returned HTTP {status} for GET {path}.", status_code=status)


@pytest.fixture
def fixed_today(monkeypatch):
    monkeypatch.setattr(tools, "_today", lambda: tools.date(2026, 11, 20))


def test_menstrual_cycle_normal_case(fixed_today):
    service = FakeService({
        "get_menstrual_cycle_summary": CYCLE_SUMMARY,
        "get_menstrual_cycle_day_view": CYCLE_DAY_VIEW,
    })
    result = run(tools.get_menstrual_cycle(service, "2026-11-10"))

    assert result["source"] == tools.SOURCE
    assert result["own_calculations"] is None
    assert result["warnings"] == []
    assert result["garmin_data"] == {
        "summary": {"dayInCycle": 12, "currentPhase": 2, "predictedCycleLength": 28},  # no account id
        "day_view": {"dayLog": {"flow": "LIGHT"}, "daySummary": {"dayInCycle": 12}},
    }
    # Both official methods, with the requested date and the server's date as todayDate.
    assert sorted((method, args) for method, args, _ in service.calls) == [
        ("get_menstrual_cycle_day_view", ("2026-11-10", "2026-11-20")),
        ("get_menstrual_cycle_summary", ("2026-11-10",)),
    ]


def test_menstrual_cycle_day_without_data_warns_instead_of_failing(fixed_today):
    service = FakeService({
        "get_menstrual_cycle_summary": None,  # empty response body
        "get_menstrual_cycle_day_view": {"userProfilePk": 99},  # nothing left once cleaned
    })
    result = run(tools.get_menstrual_cycle(service, "2026-11-10"))

    assert result["garmin_data"] == {}
    assert len(result["warnings"]) == 2
    assert all("no data for 2026-11-10" in w and "not set up" in w for w in result["warnings"])


@pytest.mark.parametrize("bad_date", ["10/11/2026", "2026-02-30", "", "today"])
def test_menstrual_cycle_rejects_invalid_dates_before_calling_garmin(bad_date):
    service = FakeService()
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        run(tools.get_menstrual_cycle(service, bad_date))
    assert service.calls == []


def test_menstrual_cycle_one_section_fails_the_other_is_returned(fixed_today):
    service = FakeService(
        {"get_menstrual_cycle_day_view": CYCLE_DAY_VIEW},
        failing={"get_menstrual_cycle_summary"},
    )
    result = run(tools.get_menstrual_cycle(service, "2026-11-10"))

    assert set(result["garmin_data"]) == {"day_view"}
    assert len(result["warnings"]) == 1 and result["warnings"][0].startswith("summary:")


@pytest.mark.parametrize("status", [400, 403, 404])
def test_menstrual_cycle_tracking_not_set_up_warns_instead_of_failing(status, fixed_today):
    service = FakeService(errors={
        "get_menstrual_cycle_summary": http_error(status),
        "get_menstrual_cycle_day_view": http_error(status),
    })
    result = run(tools.get_menstrual_cycle(service, "2026-11-10"))

    assert result["garmin_data"] == {}
    assert len(result["warnings"]) == 2
    assert all(f"HTTP {status}" in w and "not set up" in w for w in result["warnings"])


def test_menstrual_cycle_other_errors_still_fail(fixed_today):
    """An expired key (401) must not be disguised as "tracking not set up"."""
    service = FakeService(errors={
        "get_menstrual_cycle_summary": http_error(401),
        "get_menstrual_cycle_day_view": http_error(401),
    })
    with pytest.raises(GarminError, match="401"):
        run(tools.get_menstrual_cycle(service, "2026-11-10"))


def test_menstrual_cycle_logs_contain_no_response_content(monkeypatch, caplog, fixed_today):
    """Through the real MCP tool: the log shows the call, never what Garmin returned."""
    from garmin_mcp import server

    marker = "SENSITIVE-CYCLE-VALUE"
    service = FakeService({
        "get_menstrual_cycle_summary": {"feedbackPhrase": marker},
        "get_menstrual_cycle_day_view": {"dayLog": {"flow": marker}},
    })
    monkeypatch.setattr(server, "get_service", lambda: service)
    mcp = server.create_mcp_server()

    caplog.set_level(logging.INFO)
    result = run(mcp.call_tool("get_menstrual_cycle", {"date": "2026-11-10"}))

    assert not result.is_error
    assert marker in str(result.structured_content)  # the assistant does get the data
    assert "tool=get_menstrual_cycle outcome=ok" in caplog.text
    assert marker not in caplog.text


def test_menstrual_cycle_never_returns_notes_or_sexual_activity(fixed_today):
    service = FakeService({
        "get_menstrual_cycle_summary": CYCLE_SUMMARY,
        "get_menstrual_cycle_day_view": {
            "dayLog": {"flow": "LIGHT", "notes": "private text", "sexDrive": "HIGH", "sexualActivity": "YES"},
            "daySummary": {"dayInCycle": 12},
        },
    })
    result = run(tools.get_menstrual_cycle(service, "2026-11-10"))

    assert result["garmin_data"]["day_view"]["dayLog"] == {"flow": "LIGHT"}
    for field in ("notes", "sexDrive", "sexualActivity", "private text"):
        assert field not in str(result)


@pytest.mark.parametrize(
    "utc_now,spanish_date",
    [
        (tools.datetime(2026, 11, 19, 23, 30, tzinfo=tools.timezone.utc), "2026-11-20"),  # winter, UTC+1
        (tools.datetime(2026, 11, 19, 22, 30, tzinfo=tools.timezone.utc), "2026-11-19"),
        (tools.datetime(2026, 7, 1, 22, 30, tzinfo=tools.timezone.utc), "2026-07-02"),  # summer, UTC+2
        (tools.datetime(2026, 7, 1, 21, 30, tzinfo=tools.timezone.utc), "2026-07-01"),
    ],
)
def test_today_is_the_date_in_spain(utc_now, spanish_date):
    assert tools._today(utc_now).isoformat() == spanish_date
