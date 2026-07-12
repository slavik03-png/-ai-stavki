"""
Unit tests for the 36-hour event window filter and display-timezone
conversion (ai_predictions/window.py).
"""

import datetime
import sys

sys.path.insert(0, ".")

from ai_predictions.window import (
    filter_events_in_window,
    format_display_time,
    is_within_window,
    parse_commence_time,
    to_display_timezone,
)

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


NOW = datetime.datetime(2026, 7, 12, 12, 0, 0, tzinfo=datetime.timezone.utc)


def test_parse_commence_time():
    dt = parse_commence_time("2026-07-13T10:00:00Z")
    check("parses Z-suffixed ISO time", dt is not None and dt.tzinfo is not None)
    check("returns None for missing value", parse_commence_time(None) is None)
    check("returns None for garbage value", parse_commence_time("not-a-date") is None)


def test_is_within_window_boundaries():
    check("event 1h from now is in window", is_within_window(NOW + datetime.timedelta(hours=1), NOW))
    check("event exactly 36h from now is in window", is_within_window(NOW + datetime.timedelta(hours=36), NOW))
    check("event 36h+1min from now is excluded", not is_within_window(NOW + datetime.timedelta(hours=36, minutes=1), NOW))
    check("event already started is excluded", not is_within_window(NOW - datetime.timedelta(minutes=1), NOW))
    check("event starting exactly now is excluded", not is_within_window(NOW, NOW))
    check("missing commence time is excluded", not is_within_window(None, NOW))


def test_filter_events_in_window():
    events = [
        {"id": "in-window", "commence_time": (NOW + datetime.timedelta(hours=5)).isoformat()},
        {"id": "too-far", "commence_time": (NOW + datetime.timedelta(hours=40)).isoformat()},
        {"id": "already-started", "commence_time": (NOW - datetime.timedelta(hours=1)).isoformat()},
        {"id": "missing-time"},
        {"id": "bad-time", "commence_time": "garbage"},
    ]
    kept, excluded = filter_events_in_window(events, NOW)
    check("only the in-window event is kept", [e["id"] for e in kept] == ["in-window"], kept)
    check("four events were excluded (far/started/missing/bad)", excluded == 4, excluded)


def test_display_timezone_conversion():
    dt = datetime.datetime(2026, 7, 12, 12, 0, 0, tzinfo=datetime.timezone.utc)
    local = to_display_timezone(dt)
    check("Asia/Yekaterinburg is UTC+5", local.utcoffset() == datetime.timedelta(hours=5), local.utcoffset())
    text = format_display_time(dt)
    check("display text mentions Yekaterinburg", "Екатеринбург" in text, text)


def run():
    test_parse_commence_time()
    test_is_within_window_boundaries()
    test_filter_events_in_window()
    test_display_timezone_conversion()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
