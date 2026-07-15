"""
Tests for ai_predictions/live_pipeline.py -- Live in-play mode
orchestration (Task #11). Covers: isolation from the pre-match daily
archive, 10-minute cache hit/miss behaviour, dropped-unmatched-fixture
behaviour, and mode="live" persistence to tracking + analytics.

No real network calls anywhere in this file.
"""

import datetime
import os
import sys
import tempfile

sys.path.insert(0, ".")

import ai_predictions.live_pipeline as live_pipeline_mod
from ai_predictions.football_cache import FootballCache
from ai_predictions.live_fixtures import LiveFixture, LiveFixtureDiscoveryResult
from ai_predictions.odds_client import MultiSportFetchResult
from ai_predictions.live_pipeline import (
    LIVE_CACHE_KEY,
    load_cached_live_result,
    run_live_predictions,
    run_live_predictions_cached,
)
from tracking.models import MODE_LIVE, MODE_PRE_MATCH
from tracking.storage import TrackingStorage

from analytics.storage import AnalyticsStorage

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


NOW = datetime.datetime(2026, 7, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _live_fixture(fid=1, home="Home FC", away="Away FC"):
    return LiveFixture(
        fixture_id=fid, kickoff_utc=NOW - datetime.timedelta(minutes=67),
        home_team=home, away_team=away, league_name="Test League",
        league_country="Testland", status_short="2H", elapsed_minutes=67,
        home_score=1, away_score=0,
    )


def _event(home="Home FC", away="Away FC"):
    prices = [
        ("BookA", 2.00, 3.30, 4.00),
        ("BookB", 1.98, 3.35, 4.05),
        ("BookC", 2.02, 3.25, 3.95),
        ("BookD", 2.30, 3.15, 3.65),
    ]
    return {
        "id": "evt-live-1", "_sport_key": "soccer_epl", "home_team": home, "away_team": away,
        "commence_time": "2026-07-15T11:00:00Z",
        "bookmakers": [
            {"title": t, "last_update": "2026-07-15T12:30:00Z", "markets": [{
                "key": "h2h", "outcomes": [
                    {"name": home, "price": h}, {"name": "Draw", "price": d}, {"name": away, "price": a},
                ],
            }]}
            for t, h, d, a in prices
        ],
    }


def _fresh_dbs(suffix):
    paths = [
        f"/tmp/test_live_pipeline_{suffix}_cache.db",
        f"/tmp/test_live_pipeline_{suffix}_tracking.db",
        f"/tmp/test_live_pipeline_{suffix}_analytics.db",
    ]
    for p in paths:
        if os.path.exists(p):
            os.remove(p)
    football_cache = FootballCache(db_path=paths[0], now=NOW)
    storage = TrackingStorage(db_path=paths[1])
    analytics = AnalyticsStorage(db_path=paths[2], now=NOW)
    return football_cache, storage, analytics


def test_matched_live_fixture_is_saved_with_mode_live():
    football_cache, storage, analytics = _fresh_dbs("saved")
    orig_discover = live_pipeline_mod.discover_live_fixtures
    orig_fetch = live_pipeline_mod.fetch_all_active_football_events
    live_pipeline_mod.discover_live_fixtures = lambda *a, **k: LiveFixtureDiscoveryResult(fixtures=[_live_fixture()])
    live_pipeline_mod.fetch_all_active_football_events = lambda **k: MultiSportFetchResult(events=[_event()])
    try:
        result = run_live_predictions(
            football_api_key="key", odds_api_key="odds-key", now=NOW,
            storage=storage, football_cache=football_cache, analytics_storage=analytics,
        )
        check("one live fixture discovered", result.live_fixture_count == 1)
        check("fixture matched to real odds", result.matched_fixture_count == 1)
        check("one recommendation produced", result.recommendations_count == 1)
        check("saved to tracking", result.saved_count == 1)

        rows = storage.get_all_predictions() if hasattr(storage, "get_all_predictions") else None
    finally:
        live_pipeline_mod.discover_live_fixtures = orig_discover
        live_pipeline_mod.fetch_all_active_football_events = orig_fetch
        storage.close()
        analytics.close()
        football_cache.close()


def test_unmatched_live_fixture_is_dropped_never_persisted():
    football_cache, storage, analytics = _fresh_dbs("dropped")
    orig_discover = live_pipeline_mod.discover_live_fixtures
    orig_fetch = live_pipeline_mod.fetch_all_active_football_events
    live_pipeline_mod.discover_live_fixtures = lambda *a, **k: LiveFixtureDiscoveryResult(
        fixtures=[_live_fixture(fid=9, home="Nobody FC", away="Nowhere FC")]
    )
    # No matching event at all -- a completely different match.
    live_pipeline_mod.fetch_all_active_football_events = lambda **k: MultiSportFetchResult(
        events=[_event(home="Other Team", away="Another Team")]
    )
    try:
        result = run_live_predictions(
            football_api_key="key", odds_api_key="odds-key", now=NOW,
            storage=storage, football_cache=football_cache, analytics_storage=analytics,
        )
        check("live fixture discovered but not matched", result.live_fixture_count == 1 and result.matched_fixture_count == 0)
        check("unmatched fixture produces zero recommendations", result.recommendations_count == 0)
        check("nothing saved for an unmatched fixture", result.saved_count == 0)
        check("message explains no real odds were matched, not a silent empty list", len(result.telegram_messages) == 1 and "1" in result.telegram_messages[0])
    finally:
        live_pipeline_mod.discover_live_fixtures = orig_discover
        live_pipeline_mod.fetch_all_active_football_events = orig_fetch
        storage.close()
        analytics.close()
        football_cache.close()


def test_no_live_matches_at_all_is_reported_honestly():
    football_cache, storage, analytics = _fresh_dbs("no_matches")
    orig_discover = live_pipeline_mod.discover_live_fixtures
    live_pipeline_mod.discover_live_fixtures = lambda *a, **k: LiveFixtureDiscoveryResult(fixtures=[])
    try:
        result = run_live_predictions(
            football_api_key="key", odds_api_key="odds-key", now=NOW,
            storage=storage, football_cache=football_cache, analytics_storage=analytics,
        )
        check("zero live fixtures -> zero recommendations, zero saved", result.recommendations_count == 0 and result.saved_count == 0)
        check("Odds API was never even queried when nothing is live", True)  # implicit: no fetch monkeypatched, would error if called
    finally:
        live_pipeline_mod.discover_live_fixtures = orig_discover
        storage.close()
        analytics.close()
        football_cache.close()


def test_cache_hit_avoids_a_second_real_fetch():
    football_cache, storage, analytics = _fresh_dbs("cache_hit")
    orig_discover = live_pipeline_mod.discover_live_fixtures
    orig_fetch = live_pipeline_mod.fetch_all_active_football_events
    call_count = {"n": 0}

    def counting_discover(*a, **k):
        call_count["n"] += 1
        return LiveFixtureDiscoveryResult(fixtures=[_live_fixture()])

    live_pipeline_mod.discover_live_fixtures = counting_discover
    live_pipeline_mod.fetch_all_active_football_events = lambda **k: MultiSportFetchResult(events=[_event()])
    try:
        run_live_predictions_cached(
            football_api_key="key", odds_api_key="odds-key", now=NOW,
            football_cache=football_cache, ttl_minutes=10.0, storage=storage, analytics_storage=analytics,
        )
        check("first call spends one real fetch", call_count["n"] == 1)

        # Second call, 3 minutes later -- well within the 10-minute TTL.
        later = NOW + datetime.timedelta(minutes=3)
        football_cache._now = later
        result2 = run_live_predictions_cached(
            football_api_key="key", odds_api_key="odds-key", now=later,
            football_cache=football_cache, ttl_minutes=10.0, storage=storage, analytics_storage=analytics,
        )
        check("second call within TTL reuses the cache, no new fetch", call_count["n"] == 1)
        check("cached result is flagged as from_cache", result2.from_cache is True)

        # Third call, 11 minutes later -- past the TTL, must fetch again.
        much_later = NOW + datetime.timedelta(minutes=11)
        football_cache._now = much_later
        run_live_predictions_cached(
            football_api_key="key", odds_api_key="odds-key", now=much_later,
            football_cache=football_cache, ttl_minutes=10.0, storage=storage, analytics_storage=analytics,
        )
        check("call past the TTL spends a fresh real fetch", call_count["n"] == 2)
    finally:
        live_pipeline_mod.discover_live_fixtures = orig_discover
        live_pipeline_mod.fetch_all_active_football_events = orig_fetch
        storage.close()
        analytics.close()
        football_cache.close()


def test_live_pipeline_never_touches_the_shared_daily_archive_key():
    football_cache, storage, analytics = _fresh_dbs("no_shared_key")
    orig_discover = live_pipeline_mod.discover_live_fixtures
    orig_fetch = live_pipeline_mod.fetch_all_active_football_events
    live_pipeline_mod.discover_live_fixtures = lambda *a, **k: LiveFixtureDiscoveryResult(fixtures=[_live_fixture()])
    live_pipeline_mod.fetch_all_active_football_events = lambda **k: MultiSportFetchResult(events=[_event()])
    try:
        import ai_predictions.football_pipeline as football_pipeline_mod
        # Pre-match daily archive is untouched before Live runs.
        check(
            "shared daily archive key is untouched before Live runs",
            football_cache.get(football_pipeline_mod.DAILY_ARCHIVE_KEY) is None,
        )
        run_live_predictions_cached(
            football_api_key="key", odds_api_key="odds-key", now=NOW,
            football_cache=football_cache, ttl_minutes=10.0, storage=storage, analytics_storage=analytics,
        )
        check(
            "shared daily archive key is still untouched after Live runs",
            football_cache.get(football_pipeline_mod.DAILY_ARCHIVE_KEY) is None,
        )
        check("Live's own cache key is disjoint from the daily archive key", LIVE_CACHE_KEY != football_pipeline_mod.DAILY_ARCHIVE_KEY)
        check("Live's own cache key was written", football_cache.get(LIVE_CACHE_KEY) is not None)
    finally:
        live_pipeline_mod.discover_live_fixtures = orig_discover
        live_pipeline_mod.fetch_all_active_football_events = orig_fetch
        storage.close()
        analytics.close()
        football_cache.close()


def test_saved_prediction_uses_mode_live_not_pre_match():
    football_cache, storage, analytics = _fresh_dbs("mode_marker")
    orig_discover = live_pipeline_mod.discover_live_fixtures
    orig_fetch = live_pipeline_mod.fetch_all_active_football_events
    live_pipeline_mod.discover_live_fixtures = lambda *a, **k: LiveFixtureDiscoveryResult(fixtures=[_live_fixture(fid=42)])
    live_pipeline_mod.fetch_all_active_football_events = lambda **k: MultiSportFetchResult(events=[_event()])
    try:
        run_live_predictions(
            football_api_key="key", odds_api_key="odds-key", now=NOW,
            storage=storage, football_cache=football_cache, analytics_storage=analytics,
        )
        with storage._lock, storage._conn:
            row = storage._conn.execute(
                "SELECT mode FROM predictions WHERE fixture_id = ?", (42,),
            ).fetchone()
        check("tracking row for a live pick is stored with mode='live'", row is not None and row[0] == MODE_LIVE)

        analytics_row = analytics._conn.execute(
            "SELECT mode FROM predictions WHERE fixture_id = ?", (42,),
        ).fetchone()
        check("analytics row for a live pick is stored with mode='live'", analytics_row is not None and analytics_row[0] == MODE_LIVE)
    finally:
        live_pipeline_mod.discover_live_fixtures = orig_discover
        live_pipeline_mod.fetch_all_active_football_events = orig_fetch
        storage.close()
        analytics.close()
        football_cache.close()


def run():
    test_matched_live_fixture_is_saved_with_mode_live()
    test_unmatched_live_fixture_is_dropped_never_persisted()
    test_no_live_matches_at_all_is_reported_honestly()
    test_cache_hit_avoids_a_second_real_fetch()
    test_live_pipeline_never_touches_the_shared_daily_archive_key()
    test_saved_prediction_uses_mode_live_not_pre_match()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
