"""
Result-checker tests for analytics/result_checker.py -- fully mocked
API-Football responses (a fake `session.get`), no real network calls, no
real API keys, no quota spent.
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

import datetime

from ai_predictions.football_cache import FootballCache
from analytics.result_checker import fetch_fixture_result, run_check_cycle
from analytics.storage import AnalyticsStorage

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    """Replays a fixed sequence of fixture-status responses -- never
    touches the real network."""

    def __init__(self, responses):
        self._responses = responses
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return FakeResponse(response)


def _finished_payload(home_goals, away_goals, short="FT"):
    return {
        "response": [{
            "fixture": {"status": {"short": short}},
            "goals": {"home": home_goals, "away": away_goals},
            "score": {"halftime": {"home": 1, "away": 0}},
        }]
    }


def _not_started_payload():
    return {"response": [{"fixture": {"status": {"short": "NS"}}, "goals": {"home": None, "away": None}, "score": {}}]}


def _sample_prediction(**overrides):
    base = dict(
        match_datetime="2026-07-10T18:00:00+00:00", sport="football", country="England",
        league="Premier League", fixture_id=555, home_team="Arsenal", away_team="Chelsea",
        market="home_win", market_label="Победа хозяев", selection="home_win", odds=1.85,
        estimated_probability=0.58, signal_level="HIGH", reason="test", model_version="v3",
        archive_version="2026-07-10", prediction_source="api_football",
    )
    base.update(overrides)
    return base


def test_fetch_fixture_result_finished_is_cached_and_settled():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        now = datetime.datetime(2026, 7, 12, tzinfo=datetime.timezone.utc)
        cache = FootballCache(str(tmp_dir / "cache.db"), now=now)
        session = FakeSession([_finished_payload(2, 0)])

        fetched = fetch_fixture_result(555, "fake-key", cache, session=session)
        check("finished fixture returns a real result", fetched is not None and fetched["home_goals"] == 2)
        check("exactly one real HTTP call made", session.calls == 1)

        fetched_again = fetch_fixture_result(555, "fake-key", cache, session=session)
        check("second call is served from cache, no new HTTP call", session.calls == 1 and fetched_again == fetched)
        cache.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_not_yet_started_is_never_cached():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        now = datetime.datetime(2026, 7, 12, tzinfo=datetime.timezone.utc)
        cache = FootballCache(str(tmp_dir / "cache.db"), now=now)
        session = FakeSession([_not_started_payload()])
        fetched = fetch_fixture_result(555, "fake-key", cache, session=session)
        check("in-progress/not-started match returns None (not a fabricated result)", fetched is None)
        cache.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_run_check_cycle_settles_a_home_win():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        now = datetime.datetime(2026, 7, 12, tzinfo=datetime.timezone.utc)
        storage = AnalyticsStorage(str(tmp_dir / "analytics.db"), now=now)
        cache = FootballCache(str(tmp_dir / "cache.db"), now=now)

        pid = storage.record_prediction(_sample_prediction())
        check("prediction recorded before check cycle", pid is not None)

        import analytics.result_checker as rc
        original_fetch = rc.fetch_fixture_result
        rc.fetch_fixture_result = lambda fixture_id, api_key, football_cache, **kw: {
            "status": "finished", "home_goals": 2, "away_goals": 0, "ht_home_goals": 1, "ht_away_goals": 0,
        }
        try:
            summary = run_check_cycle(storage, cache, "fake-key", now, stake=100.0)
        finally:
            rc.fetch_fixture_result = original_fetch

        check("one prediction was settled", summary["settled"] == 1, summary)
        pending = storage.pending_predictions()
        check("no longer pending after settlement", len(pending) == 0)

        overall = storage.overall_stats(stake=100.0)
        check("home_win (2:0) settles as a win", overall["wins"] == 1 and overall["losses"] == 0)
        check("profit matches stake*(odds-1) = 100*0.85 = 85.0", abs(overall["profit"] - 85.0) < 0.01, overall["profit"])

        storage.close()
        cache.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_run_check_cycle_never_rechecks_a_settled_prediction():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        now = datetime.datetime(2026, 7, 12, tzinfo=datetime.timezone.utc)
        storage = AnalyticsStorage(str(tmp_dir / "analytics.db"), now=now)
        cache = FootballCache(str(tmp_dir / "cache.db"), now=now)
        storage.record_prediction(_sample_prediction())

        import analytics.result_checker as rc
        call_count = {"n": 0}

        def fake_fetch(fixture_id, api_key, football_cache, **kw):
            call_count["n"] += 1
            return {"status": "finished", "home_goals": 2, "away_goals": 0, "ht_home_goals": 1, "ht_away_goals": 0}

        original_fetch = rc.fetch_fixture_result
        rc.fetch_fixture_result = fake_fetch
        try:
            run_check_cycle(storage, cache, "fake-key", now, stake=100.0)
            run_check_cycle(storage, cache, "fake-key", now, stake=100.0)
        finally:
            rc.fetch_fixture_result = original_fetch

        check("second cycle does not re-check an already-settled prediction", call_count["n"] == 1, call_count["n"])
        storage.close()
        cache.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run():
    test_fetch_fixture_result_finished_is_cached_and_settled()
    test_not_yet_started_is_never_cached()
    test_run_check_cycle_settles_a_home_win()
    test_run_check_cycle_never_rechecks_a_settled_prediction()
    failed = [name for name, status in results if status == "FAIL"]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


if __name__ == "__main__":
    run()
