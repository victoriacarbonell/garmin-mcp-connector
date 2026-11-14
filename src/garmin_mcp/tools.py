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
from datetime import date
from typing import Any

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
})  # fmt: skip

# Range limits documented in the Swagger (days, both ends included).
RANGE_METRICS: dict[str, tuple[str, int | None]] = {
    "daily_summary": ("get_daily_summaries", 365),  # "up to a maximum of 1 year"
    "sleep": ("get_daily_sleep_summaries", 31),
    "hrv": ("get_daily_hrv_summaries", None),  # the Swagger documents no limit
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
    return _envelope(data, warnings)
