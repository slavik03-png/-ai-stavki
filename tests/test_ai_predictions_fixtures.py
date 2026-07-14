"""
Unit tests for ai_predictions/fixtures.py: real fixture discovery for the
strict 36h window. The API-Football provider is monkeypatched (a fake
provider_factory) -- no real HTTP calls happen in this file.
"""

import datetime
import sys

sys.path.insert(0, ".")

from ai_predictions.fixtures import discover_fixtures_in_window
from ai_predictions.football_cache import FootballCache

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


NOW = datetime.datetime(2026, 7, 14, 12, 0, 0, tzinfo=datetime.timezone.utc)


class _Stat:
    def __init__(self, value, available=True, reason=None):
        self.value = value
        self.available = available
        self.reason = reason


class _FakeProvider:
    def __init__(self, fixtures_by_date):
        self.fixtures_by_date = fixtures_by_date
        self.calls = []

    def get_fixtures_by_date(self, date_str):
        self.calls.append(date_str)
        return _Stat(self.fixtures_by_date.get(date_str, []))


def _raw_fixture(fixture_id, kickoff_iso, home, away, status_short="NS"):
    return {
        "fixture": {"id": fixture_id, "date": kickoff_iso, "status": {"short": status_short}},
        "league": {"name": "Premier League", "country": "England"},
        "teams": {"home": {"id": 1, "name": home}, "away": {"id": 2, "name": away}},
    }


def _fresh_cache(tmp_path):
    return FootballCache(db_path=tmp_path, now=NOW)


def test_no_api_key_returns_honest_error_not_empty_success():
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        cache = _fresh_cache(os.path.join(d, "c.db"))
        result = discover_fixtures_in_window(None, cache, NOW)
        check("missing API key produces an explicit error, not silent zero", len(result.errors) > 0, result.errors)
        check("no fixtures fabricated", result.fixtures == [])


def test_fixture_inside_window_is_discovered():
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        cache = _fresh_cache(os.path.join(d, "c.db"))
        date_str = NOW.date().isoformat()
        fixtures_by_date = {date_str: [_raw_fixture(1, (NOW + datetime.timedelta(hours=10)).isoformat(), "Home FC", "Away FC")]}
        provider = _FakeProvider(fixtures_by_date)
        result = discover_fixtures_in_window(
            "fake-key", cache, NOW, provider_factory=lambda: provider,
        )
        check("in-window fixture discovered", len(result.fixtures) == 1, result.fixtures)
        if result.fixtures:
            check("real team names preserved", result.fixtures[0].home_team == "Home FC")


def test_fixture_outside_window_excluded_and_counted():
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        cache = _fresh_cache(os.path.join(d, "c.db"))
        dates = sorted({NOW.date().isoformat(), (NOW + datetime.timedelta(hours=36)).date().isoformat(),
                        (NOW + datetime.timedelta(days=3)).date().isoformat()})
        far_date = (NOW + datetime.timedelta(days=3)).date().isoformat()
        fixtures_by_date = {far_date: [_raw_fixture(2, (NOW + datetime.timedelta(days=3)).isoformat(), "Home FC", "Away FC")]}
        provider = _FakeProvider(fixtures_by_date)
        result = discover_fixtures_in_window("fake-key", cache, NOW, provider_factory=lambda: provider)
        check("far-future fixture is never returned", result.fixtures == [])


def test_live_and_finished_fixtures_excluded_by_status():
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        cache = _fresh_cache(os.path.join(d, "c.db"))
        date_str = NOW.date().isoformat()
        fixtures_by_date = {date_str: [
            _raw_fixture(3, (NOW + datetime.timedelta(hours=5)).isoformat(), "Home FC", "Away FC", status_short="FT"),
            _raw_fixture(4, (NOW + datetime.timedelta(hours=6)).isoformat(), "Home2", "Away2", status_short="1H"),
        ]}
        provider = _FakeProvider(fixtures_by_date)
        result = discover_fixtures_in_window("fake-key", cache, NOW, provider_factory=lambda: provider)
        check("finished/live fixtures never returned as upcoming", result.fixtures == [])
        check("excluded_by_status counts both", result.excluded_by_status == 2, result.excluded_by_status)


def test_second_call_hits_cache_not_provider():
    import tempfile, os
    with tempfile.TemporaryDirectory() as d:
        cache = _fresh_cache(os.path.join(d, "c.db"))
        date_str = NOW.date().isoformat()
        fixtures_by_date = {date_str: [_raw_fixture(5, (NOW + datetime.timedelta(hours=8)).isoformat(), "Home FC", "Away FC")]}
        provider = _FakeProvider(fixtures_by_date)
        discover_fixtures_in_window("fake-key", cache, NOW, provider_factory=lambda: provider)
        calls_after_first = len(provider.calls)
        result2 = discover_fixtures_in_window("fake-key", cache, NOW, provider_factory=lambda: provider)
        check("second call for the same dates does not re-hit the provider", len(provider.calls) == calls_after_first, provider.calls)
        check("cached fixtures still returned", len(result2.fixtures) == 1)


def run():
    test_no_api_key_returns_honest_error_not_empty_success()
    test_fixture_inside_window_is_discovered()
    test_fixture_outside_window_excluded_and_counted()
    test_live_and_finished_fixtures_excluded_by_status()
    test_second_call_hits_cache_not_provider()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
