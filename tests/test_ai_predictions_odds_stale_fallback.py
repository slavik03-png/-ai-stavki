"""
Unit tests for ai_predictions/odds_client.py's stale-catalog fallback: when
The Odds API's own quota is exhausted (or the live /sports call otherwise
fails), a persisted (<=24h old) sports catalog is used instead of treating
the run as "zero football coverage", and the fallback is honestly flagged
via STALE_ODDS_MARKER rather than silently presented as fresh data.
"""

import sys

sys.path.insert(0, ".")

import ai_predictions.odds_client as odds_client_mod
from ai_predictions.odds_client import (
    QUOTA_EXHAUSTED_MARKER,
    STALE_ODDS_MARKER,
    discover_football_sport_keys,
    fetch_active_sports,
)

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


class _FakePersistentCache:
    def __init__(self, stored=None):
        self.stored = stored
        self.set_calls = []

    def get(self, key):
        return self.stored

    def set(self, key, value):
        self.set_calls.append((key, value))
        self.stored = value


def test_quota_exhausted_falls_back_to_stale_persisted_catalog():
    stale_catalog = [{"key": "soccer_epl", "group": "Soccer", "active": True, "has_outrights": False}]
    cache = _FakePersistentCache(stored=stale_catalog)

    class _FakeResponse:
        status_code = 401
        text = '{"error_code":"OUT_OF_USAGE_CREDITS"}'

    original_get = odds_client_mod.requests.get
    original_cache_get = odds_client_mod._cache_get_sports_list
    odds_client_mod.requests.get = lambda *a, **k: _FakeResponse()
    odds_client_mod._cache_get_sports_list = lambda: None
    try:
        sports, error = fetch_active_sports(api_key="fake-key", persistent_cache=cache)
    finally:
        odds_client_mod.requests.get = original_get
        odds_client_mod._cache_get_sports_list = original_cache_get

    check("stale catalog returned instead of None on quota exhaustion", sports == stale_catalog, sports)
    check("error honestly flags the stale fallback", error is not None and STALE_ODDS_MARKER in error, error)
    check("original quota-exhausted reason preserved in the message", error is not None and QUOTA_EXHAUSTED_MARKER in error, error)


def test_no_persisted_catalog_means_honest_failure_not_fabrication():
    cache = _FakePersistentCache(stored=None)

    class _FakeResponse:
        status_code = 401
        text = '{"error_code":"OUT_OF_USAGE_CREDITS"}'

    original_get = odds_client_mod.requests.get
    original_cache_get = odds_client_mod._cache_get_sports_list
    odds_client_mod.requests.get = lambda *a, **k: _FakeResponse()
    odds_client_mod._cache_get_sports_list = lambda: None
    try:
        sports, error = fetch_active_sports(api_key="fake-key", persistent_cache=cache)
    finally:
        odds_client_mod.requests.get = original_get
        odds_client_mod._cache_get_sports_list = original_cache_get

    check("no stale data available -> None, never invented", sports is None)
    check("error still names the quota exhaustion", error is not None and QUOTA_EXHAUSTED_MARKER in error, error)


def test_discovery_propagates_stale_marker_through_the_api_source_path():
    stale_catalog = [{"key": "soccer_epl", "group": "Soccer", "active": True, "has_outrights": False}]
    cache = _FakePersistentCache(stored=stale_catalog)
    original_fetch = odds_client_mod.fetch_active_sports
    odds_client_mod.fetch_active_sports = lambda api_key=None, persistent_cache=None: (
        stale_catalog, f"{STALE_ODDS_MARKER}: {QUOTA_EXHAUSTED_MARKER}: квота исчерпана",
    )
    try:
        discovery = discover_football_sport_keys(api_key="fake-key", persistent_cache=cache)
    finally:
        odds_client_mod.fetch_active_sports = original_fetch

    check("stale catalog still used as the real source, not hardcoded fallback", discovery.source == "api", discovery.source)
    check("soccer_epl still discovered from the stale catalog", "soccer_epl" in discovery.included, discovery.included)
    check("discovery_error carries the stale marker for honest diagnostics",
          discovery.discovery_error is not None and STALE_ODDS_MARKER in discovery.discovery_error, discovery.discovery_error)


def run():
    test_quota_exhausted_falls_back_to_stale_persisted_catalog()
    test_no_persisted_catalog_means_honest_failure_not_fabrication()
    test_discovery_propagates_stale_marker_through_the_api_source_path()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
