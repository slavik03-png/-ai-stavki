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


def format_card_time(dt: datetime.datetime) -> str:
    """Short human time for the user-facing prediction card and the daily
    archive header -- Yekaterinburg local time, no explicit city label
    (the bot's audience is fixed to that timezone). Kept separate from
    format_display_time() above, which existing diagnostics/tests rely on."""
    local = to_display_timezone(dt)
    return local.strftime("%d.%m.%Y в %H:%M")


def is_same_local_day(dt_a: datetime.datetime, dt_b: datetime.datetime) -> bool:
    """True only if both timestamps fall on the same Yekaterinburg
    calendar date. This is the one place that must decide "is this still
    today" for anything day-scoped (the daily archive, in particular) --
    comparing raw UTC timestamps or elapsed hours is not equivalent,
    since a UTC day and a Yekaterinburg day do not align (UTC+5)."""
    return to_display_timezone(dt_a).date() == to_display_timezone(dt_b).date()


def local_date_str(dt: datetime.datetime) -> str:
    """ISO calendar date (YYYY-MM-DD) in Yekaterinburg local time -- used
    for logging/diagnostics so a date comparison is unambiguous."""
    return to_display_timezone(dt).date().isoformat()


def format_user_time(dt: datetime.datetime, now: Optional[datetime.datetime] = None) -> str:
    """The one timestamp formatter for anything a user reads (reports,
    status, "updated at" lines): always Yekaterinburg local time, never
    UTC. Falls back to a friendlier "сегодня в ЧЧ:ММ" when `dt` falls on
    the same Yekaterinburg calendar date as `now` (defaults to the
    current moment); otherwise "ДД.ММ.ГГГГ ЧЧ:ММ". UTC must stay confined
    to logs/diagnostics -- never pass a UTC-labelled string to the user."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    local = to_display_timezone(dt)
    local_now = to_display_timezone(now)
    if local.date() == local_now.date():
        return "сегодня в " + local.strftime("%H:%M")
    return local.strftime("%d.%m.%Y %H:%M")
