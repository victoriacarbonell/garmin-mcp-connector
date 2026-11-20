"""MCP server: exposes the functions in `tools.py` as tools.

SDK note: in `mcp` 2.x the `FastMCP` class is called `MCPServer` (same idea:
you decorate a function and the SDK builds the schema from the type hints and
the docstring).

The docstrings are what the assistant reads to decide which tool to use, so
they describe when to use each one and the format of its parameters.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import tools
from .config import load_settings
from .garmin import GarminError, GarminService

logger = logging.getLogger("garmin_mcp")

# Every tool is read-only and queries an external service. MCP clients use
# these hints to, for example, skip confirmation prompts.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)

INSTRUCTIONS = (
    "Personal Garmin Connect data, read-only. Every response separates "
    "'garmin_data' (values exactly as Garmin reports them) from 'own_calculations' "
    "(computed by this connector; null when there are none). Never present an own "
    "calculation as if it were Garmin data. Dates use the YYYY-MM-DD format."
)


@lru_cache(maxsize=1)
def get_service() -> GarminService:
    """Created on first use, not at import time, so tests do not need a key."""
    settings = load_settings()
    return GarminService(settings.garmin_api_key, cache_ttl_seconds=settings.cache_ttl_seconds)


async def _run(name: str, call: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Run a tool and log ONLY its name, outcome and duration.

    Neither arguments nor responses go to the log: they hold dates, IDs and health data.
    """
    started = time.monotonic()
    outcome = "ok"
    try:
        return await call()
    except (GarminError, ValueError) as exc:
        outcome = type(exc).__name__
        # ToolError reaches the assistant as a readable tool error.
        raise ToolError(str(exc)) from None
    except Exception as exc:
        outcome = type(exc).__name__
        raise ToolError("Internal connector error.") from None
    finally:
        logger.info("tool=%s outcome=%s ms=%d", name, outcome, (time.monotonic() - started) * 1000)


def create_mcp_server(**auth_options: Any) -> MCPServer:
    """Build the MCP server and register its tools.

    `auth_options` are passed straight to MCPServer (`auth=` and
    `auth_server_provider=`); with none, the server has no authorization.
    """
    mcp = MCPServer("garmin", title="Garmin (personal)", instructions=INSTRUCTIONS, **auth_options)

    @mcp.tool(title="List activities", annotations=READ_ONLY)
    async def list_activities(
        start_date: str,
        end_date: str,
        activity_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List the activities recorded between two dates (both included, YYYY-MM-DD).

        activity_type filters by Garmin "parent" type (for example "running"); subtypes are
        not accepted. If you do not know the exact key for a sport, list without a filter and
        look at the activityType field of the results. Garmin units: distance in meters,
        durations in seconds, speed in meters per second. Use offset to paginate.
        """
        return await _run(
            "list_activities",
            lambda: tools.list_activities(get_service(), start_date, end_date, activity_type, limit, offset),
        )


    @mcp.tool(title="Activity details", annotations=READ_ONLY)
    async def get_activity(activity_id: int, include_workouts: bool = False) -> dict[str, Any]:
        """Return the details of one activity: summary, metadata, measurements and splits.

        The activity_id comes from list_activities. include_workouts adds the workouts
        performed during the activity.
        """
        return await _run(
            "get_activity", lambda: tools.get_activity(get_service(), activity_id, include_workouts)
        )


    @mcp.tool(title="Health for one day", annotations=READ_ONLY)
    async def get_daily_health(date: str, include_hrv_readings: bool = False) -> dict[str, Any]:
        """Health data for one day (YYYY-MM-DD): daily summary (steps, calories, resting
        heart rate, stress, Body Battery), that night's sleep and HRV.

        If one section fails, the others are still returned and the failure is listed in
        'warnings'. include_hrv_readings adds the 5-minute HRV readings (a lot of volume).
        """
        return await _run(
            "get_daily_health", lambda: tools.get_daily_health(get_service(), date, include_hrv_readings)
        )


    @mcp.tool(title="Health over a date range", annotations=READ_ONLY)
    async def get_health_range(
        start_date: str, end_date: str, metrics: list[str] | None = None
    ) -> dict[str, Any]:
        """Daily health summaries between two dates (both included), to look at trends.

        metrics: a list with "daily_summary" (max 365 days), "sleep" (max 31 days) and/or
        "hrv". If omitted, all three are requested, and then the range cannot exceed 31 days.
        """
        return await _run(
            "get_health_range", lambda: tools.get_health_range(get_service(), start_date, end_date, metrics)
        )

    @mcp.tool(title="Menstrual cycle for one day", annotations=READ_ONLY)
    async def get_menstrual_cycle(date: str) -> dict[str, Any]:
        """Menstrual cycle data for one date (YYYY-MM-DD), as recorded and predicted by Garmin.

        Use it to answer questions about a specific date: the cycle phase, the day within
        the cycle, and Garmin's predictions (cycle length, ovulation date, fertile window,
        days until the next phase). The date may be in the past or the future.

        It covers ONE date per call. To look at several days, call it once per day.

        Returns two sections: 'summary' (the cycle that contains the date) and 'day_view'
        (what was logged on that date plus the summary for that day). The phase is given
        as a numeric id (currentPhase) whose meaning Garmin does not document: do not
        guess a phase name from the number. If cycle tracking is not set up in Garmin
        Connect, the sections are missing and 'warnings' explains why.

        Free-text notes and sexual activity fields are removed on purpose and are never
        returned: their absence does not mean nothing was logged.
        """
        return await _run("get_menstrual_cycle", lambda: tools.get_menstrual_cycle(get_service(), date))

    return mcp
