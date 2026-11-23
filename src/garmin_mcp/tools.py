"""Logic of the MVP tools (phase 1).

This module knows nothing about MCP: these are plain functions that take a
`GarminService` and return dictionaries. That makes them testable with pytest
without starting a server, and `server.py` only has to expose them.

Every response has the same shape, to follow the project rule of separating
Garmin data from our own calculations:

    {
      "source": "Garmin Connect Personal API",
      "garmin_data": {...},         # as returned by Garmin (trimmed, values untouched)
      "own_calculations": None,     # phase 1 computes nothing
      "warnings": [...]             # e.g. a section that failed
    }

Field names used here come from the official Swagger (v1.0.0).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .garmin import GarminError, GarminService

SOURCE = "Garmin Connect Personal API"

# Activity list fields we keep. The Swagger documents ~190 fields per activity
# (diving, cycling power, social data...): returning all of them would burn
# assistant context for nothing.
ACTIVITY_LIST_FIELDS = (
    "activityId", "activityName", "startTimeLocal", "activityType", "locationName",
    "distance", "duration", "movingDuration", "elevationGain", "averageSpeed",
    "calories", "averageHR", "maxHR", "steps",
    "aerobicTrainingEffect", "anaerobicTrainingEffect", "trainingEffectLabel",
    "activityTrainingLoad", "moderateIntensityMinutes", "vigorousIntensityMinutes",
    "differenceBodyBattery", "totalSets", "totalReps", "vO2MaxValue",
)  # fmt: skip

# Account identifiers and social data that no response needs.
_DROP_KEYS = frozenset({
    "userProfilePK", "userProfilePk", "userProfileId", "ownerId", "ownerDisplayName",
    "ownerFullName", "ownerProfileImageUrlSmall", "ownerProfileImageUrlMedium",
    "ownerProfileImageUrlLarge",
    # Activity details metadata: full name, display name and profile image URLs.
    "userInfoDto",
})  # fmt: skip

# Range limits documented in the Swagger (days, both ends included).
RANGE_METRICS: dict[str, tuple[str, int | None]] = {
    "daily_summary": ("get_daily_summaries", 365),  # "up to a maximum of 1 year"
    "sleep": ("get_daily_sleep_summaries", 31),
    "hrv": ("get_daily_hrv_summaries", None),  # the Swagger documents no limit
}

# Fields get_health_range keeps for each day, as a nested spec: a set lists the
# keys kept from a dict, a dict maps a key to the spec of its value. A full day
# is ~6,000 characters (feedback messages, "optimal" ranges, timestamps...); a
# 31-day range would use a large share of the assistant's context just to look
# at trends. get_daily_health still returns one day in full. Names come from
# real responses of the Personal API.
RANGE_FIELDS: dict[str, Any] = {
    "daily_summary": {
        "calendarDate": None,
        "movement": {
            "steps": {"value", "goal", "distanceInMeters"},
            "floorsAscended": {"value"},
        },
        "calories": {"burnedTotal", "burnedActive"},
        "heartRate": {"restingValue", "minValue", "maxValue"},
        "stress": {"avgLevel", "maxLevel", "qualifier"},
        "bodyBattery": {"minValue", "maxValue", "chargedValue", "drainedValue"},
        "intensityMinutes": {"moderate", "vigorous"},
        "pulseOx": {"avgValue"},
        "respiration": {"avgValue"},
    },
    "sleep": {
        "calendarDate": None,
        "sleepTimeSeconds": None,
        "deepSleepSeconds": None,
        "lightSleepSeconds": None,
        "remSleepSeconds": None,
        "awakeSleepSeconds": None,
        "awakeCount": None,
        "avgHeartRate": None,
        "averageSpO2Value": None,
        "averageRespirationValue": None,
        "avgSleepStress": None,
        "sleepScores": {"overall": {"value", "qualifierKey"}},
    },
    # Already small (~300 characters a day): returned as Garmin sends it.
    "hrv": None,
}


def _parse_date(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must use the YYYY-MM-DD format (got: {value!r}).") from None


def _parse_range(start_date: str, end_date: str) -> tuple[date, date, int]:
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    if end < start:
        raise ValueError("end_date cannot be earlier than start_date.")
    return start, end, (end - start).days + 1


def _clean(value: Any) -> Any:
    """Remove null values and account identifiers. Never alters a value."""
    if isinstance(value, dict):
        return {
            k: _clean(v) for k, v in value.items() if v is not None and k not in _DROP_KEYS
        }
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _pick(value: Any, spec: Any) -> Any:
    """Keep only the fields named in `spec` (see RANGE_FIELDS). Never alters a value.

    Lists are walked item by item. A dict whose keys the spec does not name is
    treated as a container (e.g. {"data": [...]}) and each of its values is picked.
    """
    if spec is None:
        return value
    if isinstance(value, list):
        return [_pick(v, spec) for v in value]
    if not isinstance(value, dict):
        return value
    names = spec if isinstance(spec, (set, frozenset)) else set(spec)
    if not names & value.keys():
        return {k: _pick(v, spec) for k, v in value.items() if isinstance(v, (list, dict))}
    if isinstance(spec, dict):
        return {k: _pick(value[k], sub) for k, sub in spec.items() if k in value}
    return {k: value[k] for k in spec if k in value}


def _envelope(data: Any, warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "source": SOURCE,
        "garmin_data": data,
        "own_calculations": None,
        "warnings": warnings or [],
    }


async def _sections(calls: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Run several queries and tolerate some of them failing (noted in `warnings`)."""
    results = await asyncio.gather(*calls.values(), return_exceptions=True)
    data: dict[str, Any] = {}
    warnings: list[str] = []
    for name, result in zip(calls, results):
        if isinstance(result, GarminError):
            warnings.append(f"{name}: {result}")
        elif isinstance(result, BaseException):
            raise result
        else:
            data[name] = _clean(result)
    if not data and warnings:
        raise GarminError(" | ".join(warnings))
    return data, warnings


async def list_activities(
    service: GarminService,
    start_date: str,
    end_date: str,
    activity_type: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    start, end, _ = _parse_range(start_date, end_date)
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100.")
    if offset < 0:
        raise ValueError("offset cannot be negative.")
    raw = await service.call(
        "search_activities",
        start=offset,
        limit=limit,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        activity_type=activity_type or None,
    )
    activities = [
        _clean({k: a[k] for k in ACTIVITY_LIST_FIELDS if k in a})
        for a in (raw or [])
        if isinstance(a, dict)
    ]
    warnings = []
    if len(activities) == limit:
        warnings.append(
            f"Limit reached ({limit}). There may be more: repeat with offset={offset + limit}."
        )
    return _envelope({"activities": activities}, warnings)


async def get_activity(
    service: GarminService, activity_id: int, include_workouts: bool = False
) -> dict[str, Any]:
    if activity_id <= 0:
        raise ValueError("activity_id must be a positive integer.")
    calls = {"activity": service.call("get_activity", activity_id)}
    if include_workouts:
        calls["workouts"] = service.call("get_activity_workouts", activity_id)
    data, warnings = await _sections(calls)
    return _envelope(data, warnings)


async def get_daily_health(
    service: GarminService, day: str, include_hrv_readings: bool = False
) -> dict[str, Any]:
    d = _parse_date(day, "date").isoformat()
    data, warnings = await _sections({
        # Daily summary: steps, calories, heart rate (incl. resting), stress, Body Battery...
        "daily_summary": service.call("get_daily_summaries", d, d),
        "sleep": service.call("get_daily_sleep", d),
        "hrv": service.call("get_daily_hrv", d),
    })
    hrv = data.get("hrv")
    if not include_hrv_readings and isinstance(hrv, dict) and "hrvReadings" in hrv:
        # Readings every 5 minutes through the night: a lot of volume. On demand only.
        del hrv["hrvReadings"]
        warnings.append("hrv: 5-minute readings omitted (pass include_hrv_readings=true to get them).")
    return _envelope(data, warnings)


async def get_health_range(
    service: GarminService, start_date: str, end_date: str, metrics: list[str] | None = None
) -> dict[str, Any]:
    start, end, days = _parse_range(start_date, end_date)
    selected = metrics or list(RANGE_METRICS)
    unknown = [m for m in selected if m not in RANGE_METRICS]
    if unknown:
        raise ValueError(f"Invalid metrics: {unknown}. Options: {list(RANGE_METRICS)}.")
    for metric in selected:
        max_days = RANGE_METRICS[metric][1]
        if max_days is not None and days > max_days:
            raise ValueError(
                f"'{metric}' allows at most {max_days} days per query (Garmin limit); "
                f"{days} were requested. Split the range into several calls."
            )
    data, warnings = await _sections({
        m: service.call(RANGE_METRICS[m][0], start.isoformat(), end.isoformat()) for m in selected
    })
    return _envelope({m: _pick(v, RANGE_FIELDS[m]) for m, v in data.items()}, warnings)


# HTTP statuses that, for the menstrual cycle endpoints, most likely mean that
# cycle tracking is not set up for the account. Garmin does not document this:
# it is our reading of 400/403/404. Other errors (401 expired key, 429, 5xx,
# network) are handled like in every other tool.
CYCLE_NOT_SET_UP_STATUSES = frozenset({400, 403, 404})
_CYCLE_NOT_SET_UP_HINT = (
    "This may mean menstrual cycle tracking is not set up in Garmin Connect for this account."
)


class _CycleNotSetUp:
    """Marks a section whose request failed in a way that suggests tracking is off."""

    def __init__(self, error: GarminError) -> None:
        self.error = error


async def _cycle_section(call: Any) -> Any:
    try:
        return await call
    except GarminError as exc:
        if exc.status_code in CYCLE_NOT_SET_UP_STATUSES:
            return _CycleNotSetUp(exc)
        raise


# The owner lives in Spain. The server runs in UTC on Azure, so "today" is
# computed in Spanish time; otherwise it would still be yesterday between
# midnight and 01:00 (02:00 in summer).
OWNER_TIMEZONE = ZoneInfo("Europe/Madrid")

# Fields of the day log that are deliberately never returned: free-text notes and
# sexual activity. The owner chose not to send them to any AI assistant.
CYCLE_PRIVATE_FIELDS = frozenset({"notes", "sexDrive", "sexualActivity"})


def _today(now: datetime | None = None) -> date:
    """Today's date in Spain. `now` (timezone-aware) is only passed by tests."""
    return (now or datetime.now(timezone.utc)).astimezone(OWNER_TIMEZONE).date()


def _drop_private_cycle_fields(value: Any) -> Any:
    """Remove CYCLE_PRIVATE_FIELDS at any depth, wherever Garmin puts them."""
    if isinstance(value, dict):
        return {k: _drop_private_cycle_fields(v) for k, v in value.items() if k not in CYCLE_PRIVATE_FIELDS}
    if isinstance(value, list):
        return [_drop_private_cycle_fields(v) for v in value]
    return value


async def get_menstrual_cycle(service: GarminService, day: str) -> dict[str, Any]:
    d = _parse_date(day, "date").isoformat()
    data, warnings = await _sections({
        # Cycle summary for that date: phase, day in cycle, predictions...
        "summary": _cycle_section(service.call("get_menstrual_cycle_summary", d)),
        # Details of that date: logged data, the summary for that day and, for
        # pregnancy tracking, weight goals. `todayDate` anchors the predictions.
        "day_view": _cycle_section(
            service.call("get_menstrual_cycle_day_view", d, _today().isoformat())
        ),
    })
    for name in list(data):
        value = data[name] = _drop_private_cycle_fields(data[name])
        if isinstance(value, _CycleNotSetUp):
            del data[name]
            warnings.append(f"{name}: {value.error} {_CYCLE_NOT_SET_UP_HINT}")
        elif value is None or value == {} or value == []:
            del data[name]
            warnings.append(f"{name}: Garmin returned no data for {d}. {_CYCLE_NOT_SET_UP_HINT}")
    return _envelope(data, warnings)
