"""
Unit tests for ai_predictions/live_fixtures.py -- Live in-play fixture
discovery (Task #11).
"""

import datetime
import sys

sys.path.insert(0, ".")

from ai_predictions.football_cache import FootballCache
from ai_predictions.live_fixtures import discover_live_fixtures
from football.interface import Stat

NOW = datetime.datetime(2026, 7, 15, 12, 0, tzinfo=datetime.timezone.utc)

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def _cache(tmp_path_suffix: str) -> FootballCache:
    return FootballCache(db_path=f"/tmp/test_live_fixtures_{tmp_path_suffix}.db", now=NOW)


def _raw_fixture(fixture_id, status_short, elapsed=50, home="Team A", away="Team B"):
    return {
        "fixture": {
            "id": fixture_id,
            "date": "2026-07-15T11:00:00+00:00",
            "status": {"short": status_short, "elapsed": elapsed},
        },
        "league": {"name": "Test League", "country": "Testland"},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "goals": {"home": 1, "away": 0},
    }


class FakeProvider:
    def __init__(self, raw_fixtures=None, stat=None):
        self._raw_fixtures = raw_fixtures or []
        self._stat = stat

    def get_live_fixtures(self):
        if self._stat is not None:
            return self._stat
        return Stat.ok(self._raw_fixtures)


def test_no_api_key_returns_error_never_zero_matches():
    cache = _cache("no_key")
    result = discover_live_fixtures(None, cache)
    check("no API key -> errors, not a silent empty result", bool(result.errors) and not result.ok)
    cache.close()


def test_real_error_is_never_treated_as_zero_matches():
    cache = _cache("real_error")
    provider = FakeProvider(stat=Stat.missing("лимит запросов превышен"))
    result = discover_live_fixtures("key", cache, provider_factory=lambda: provider)
    check("failed call -> errors recorded, ok=False", not result.ok and result.errors == ["лимит запросов превышен"])
    check("failed call -> zero fixtures returned (never fabricated)", result.fixtures == [])
    cache.close()


def test_live_statuses_are_included_others_excluded():
    cache = _cache("statuses")
    raw = [
        _raw_fixture(1, "1H"),
        _raw_fixture(2, "HT"),
        _raw_fixture(3, "NS"),   # not started -- must be excluded
        _raw_fixture(4, "FT"),   # finished -- must be excluded
        _raw_fixture(5, "2H"),
    ]
    provider = FakeProvider(raw_fixtures=raw)
    result = discover_live_fixtures("key", cache, provider_factory=lambda: provider)
    ids = sorted(fx.fixture_id for fx in result.fixtures)
    check("only real live-status fixtures kept", ids == [1, 2, 5], ids)
    check("non-live statuses counted as excluded, not silently dropped", result.excluded_missing_fields == 2)
    cache.close()


def test_missing_required_fields_excluded_not_guessed():
    cache = _cache("missing_fields")
    bad = _raw_fixture(6, "1H")
    bad["teams"]["home"] = {}  # no team name -> cannot be trusted
    provider = FakeProvider(raw_fixtures=[bad])
    result = discover_live_fixtures("key", cache, provider_factory=lambda: provider)
    check("fixture missing a team name is excluded, not included with a blank name", result.fixtures == [])
    cache.close()


def test_quota_reserve_shared_with_pre_match_pipeline():
    cache = _cache("quota")
    # Exhaust the reserve entirely.
    cache.record_requests(10_000)
    provider = FakeProvider(raw_fixtures=[_raw_fixture(7, "1H")])
    result = discover_live_fixtures("key", cache, provider_factory=lambda: provider)
    check("live discovery respects the shared quota reserve", not result.ok and "квота" in result.errors[0].lower() or "резерв" in result.errors[0].lower())
    cache.close()


def run():
    test_no_api_key_returns_error_never_zero_matches()
    test_real_error_is_never_treated_as_zero_matches()
    test_live_statuses_are_included_others_excluded()
    test_missing_required_fields_excluded_not_guessed()
    test_quota_reserve_shared_with_pre_match_pipeline()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
