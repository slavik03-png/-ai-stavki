"""
36-hour event window (spec section on event horizon) and display-timezone
conversion. All internal comparisons stay in UTC; Asia/Yekaterinburg is
only used when rendering a time for a human to read.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

WINDOW_HOURS = 36
DISPLAY_TZ = ZoneInfo("Asia/Yekaterinburg")


def parse_commence_time(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def is_within_window(
    commence_dt: Optional[datetime.datetime],
    now: datetime.datetime,
    window_hours: float = WINDOW_HOURS,
) -> bool:
    """True only for events that have not started yet (`commence_dt > now`)
    and start no later than `window_hours` from `now`. A missing/invalid
    `commence_dt` is never assumed to be "in window" -- it is excluded."""
    if commence_dt is None:
        return False
    if commence_dt <= now:
        return False
    return commence_dt <= now + datetime.timedelta(hours=window_hours)


def filter_events_in_window(
    events: List[Dict[str, Any]],
    now: datetime.datetime,
    window_hours: float = WINDOW_HOURS,
    *,
    time_field: str = "commence_time",
) -> Tuple[List[Dict[str, Any]], int]:
    """Returns (events_in_window, excluded_count). Events with a missing or
    unparsable start time are excluded and counted, never guessed."""
    kept = []
    excluded = 0
    for event in events:
        commence_dt = parse_commence_time(event.get(time_field))
        if is_within_window(commence_dt, now, window_hours):
            kept.append(event)
        else:
            excluded += 1
    return kept, excluded


def to_display_timezone(dt: datetime.datetime) -> datetime.datetime:
    return dt.astimezone(DISPLAY_TZ)


def format_display_time(dt: datetime.datetime) -> str:
    local = to_display_timezone(dt)
    return local.strftime("%d.%m.%Y %H:%M") + " (Екатеринбург)"
