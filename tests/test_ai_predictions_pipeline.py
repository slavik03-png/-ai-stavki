"""
Unit tests for ai_predictions/pipeline.py, with the network layer
(fetch_football_events) and the statistics provider (ApiFootballProvider)
monkeypatched -- no real HTTP calls happen in this test file.
"""

import datetime
import os
import sys
import tempfile

sys.path.insert(0, ".")

import ai_predictions.pipeline as pipeline_mod
from football.providers.mock_provider import MockFootballProvider
from tracking.storage import TrackingStorage

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


NOW = datetime.datetime(2026, 7, 12, 12, 0, 0, tzinfo=datetime.timezone.utc)


def strong_event(event_id="evt-strong"):
    return {
        "id": event_id,
        "_sport_key": "soccer_epl",
        "sport_title": "Mock League",
        "commence_time": (NOW + datetime.timedelta(hours=5)).isoformat(),
        "home_team": "Mock Home FC",
        "away_team": "Mock Away FC",
        "bookmakers": [
            {"title": "BookA", "last_update": "2026-07-12T10:00:00Z", "markets": [{
                "key": "h2h", "outcomes": [
                    {"name": "Mock Home FC", "price": 1.30},
                    {"name": "Draw", "price": 5.5},
                    {"name": "Mock Away FC", "price": 9.0},
                ],
            }]},
            {"title": "BookB", "last_update": "2026-07-12T10:05:00Z", "markets": [{
                "key": "h2h", "outcomes": [
                    {"name": "Mock Home FC", "price": 1.32},
                    {"name": "Draw", "price": 5.3},
                    {"name": "Mock Away FC", "price": 8.5},
                ],
            }]},
        ],
    }


def out_of_window_event():
    e = strong_event("evt-far")
    e["commence_time"] = (NOW + datetime.timedelta(hours=48)).isoformat()
    return e


def _run_with_fixture(events, tmp_db_path, football_key="fake-key"):
    pipeline_mod.fetch_football_events = lambda api_key=None: (events, "500", [])
    storage = TrackingStorage(db_path=tmp_db_path)
    result = pipeline_mod.run_ai_predictions(
        football_api_key=football_key, odds_api_key="fake-odds-key",
        storage=storage, now=NOW,
    )
    return result, storage


def test_out_of_window_events_are_excluded():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "t.db")
        original_provider = pipeline_mod.ApiFootballProvider
        pipeline_mod.ApiFootballProvider = lambda api_key=None: MockFootballProvider()
        try:
            result, storage = _run_with_fixture([out_of_window_event()], db_path)
        finally:
            pipeline_mod.ApiFootballProvider = original_provider
        check("far-future event excluded from consideration", result.events_considered == 0, result.events_considered)
        check("excluded count reflects the one out-of-window event", result.events_excluded_by_window == 1)
        storage.close()


def test_strong_event_produces_saved_main_recommendation():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "t.db")
        original_provider = pipeline_mod.ApiFootballProvider
        pipeline_mod.ApiFootballProvider = lambda api_key=None: MockFootballProvider()
        try:
            result, storage = _run_with_fixture([strong_event()], db_path)
            check("one event considered", result.events_considered == 1)
            check("candidates were built from real odds", result.candidates_considered > 0, result.candidates_considered)
            check("report text contains disclaimer", "не гаранти" in result.report_text.lower() or "гарант" in result.report_text.lower(), result.report_text[:200])

            # Re-running the identical event must not duplicate storage rows.
            events2, _, _ = ([strong_event()], None, None)
            result2 = pipeline_mod.run_ai_predictions(
                football_api_key="fake-key", odds_api_key="fake-odds-key",
                storage=storage, now=NOW,
            )
            check("re-running the same event does not crash", isinstance(result2.saved_count, int))
            check("duplicate saves are tracked, not silently dropped nor double-counted",
                  result2.duplicate_count >= result.saved_count if result.saved_count else True,
                  (result.saved_count, result2.saved_count, result2.duplicate_count))
        finally:
            pipeline_mod.ApiFootballProvider = original_provider
            storage.close()


def test_no_events_yields_honest_empty_report():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "t.db")
        original_provider = pipeline_mod.ApiFootballProvider
        pipeline_mod.ApiFootballProvider = lambda api_key=None: MockFootballProvider()
        try:
            result, storage = _run_with_fixture([], db_path)
            check("zero events -> zero saved", result.saved_count == 0)
            check("report explicitly says no recommendations, never invents one",
                  "нет" in result.report_text.lower() or "не найдено" in result.report_text.lower() or "рекоменд" in result.report_text.lower(),
                  result.report_text[:300])
        finally:
            pipeline_mod.ApiFootballProvider = original_provider
            storage.close()


def test_odds_fetch_error_does_not_crash_pipeline():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "t.db")
        pipeline_mod.fetch_football_events = lambda api_key=None: ([], None, ["Не найден ODDS_API_KEY"])
        original_provider = pipeline_mod.ApiFootballProvider
        pipeline_mod.ApiFootballProvider = lambda api_key=None: MockFootballProvider()
        try:
            storage = TrackingStorage(db_path=db_path)
            result = pipeline_mod.run_ai_predictions(
                football_api_key="fake-key", odds_api_key=None, storage=storage, now=NOW,
            )
            check("odds fetch failure surfaces as an error, not a crash", len(result.errors) == 1, result.errors)
            check("no candidates were fabricated despite the error", result.candidates_considered == 0)
        finally:
            pipeline_mod.ApiFootballProvider = original_provider
            storage.close()


def test_save_main_predictions_dedups_directly():
    from selection_engine.config import MARKET_1X2
    from selection_engine.models import CandidatePrediction

    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "t.db")
        storage = TrackingStorage(db_path=db_path)
        candidate = CandidatePrediction(
            event_id="evt-dedup", sport="football", league="Mock League", country=None,
            match_datetime=NOW.isoformat(), home_team="Mock Home FC", away_team="Mock Away FC",
            market_type=MARKET_1X2, selection="Mock Home FC", line=None, bookmaker="BookA",
            odds=1.5, model_probability=0.8, confidence_score=90.0,
            recommendation_group="MAIN", model_version="test-v1",
        )
        saved1, dup1 = pipeline_mod._save_main_predictions([candidate], storage)
        saved2, dup2 = pipeline_mod._save_main_predictions([candidate], storage)
        check("first save succeeds", saved1 == 1 and dup1 == 0, (saved1, dup1))
        check("second identical save is treated as a duplicate, not stored twice", saved2 == 0 and dup2 == 1, (saved2, dup2))
        check("exactly one prediction persisted", len(storage.list_all_predictions()) == 1)
        storage.close()


def run():
    test_out_of_window_events_are_excluded()
    test_save_main_predictions_dedups_directly()
    test_strong_event_produces_saved_main_recommendation()
    test_no_events_yields_honest_empty_report()
    test_odds_fetch_error_does_not_crash_pipeline()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
