"""
Tests for tracking/result_checker.py: mock provider architecture, and that
running a settlement cycle repeatedly never settles the same prediction
twice. No real network requests are made anywhere in this file.
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from tracking.models import Prediction, EventResult, STATUS_WON, STATUS_PENDING
from tracking.storage import TrackingStorage
from tracking.result_checker import MockResultProvider, run_settlement_cycle

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def _prediction(**overrides) -> Prediction:
    base = dict(
        sport="football", country="England", league="Premier League",
        event_id="evt-past", event_start_time="2026-07-01T18:00:00+00:00",
        home_team="Arsenal", away_team="Chelsea",
        market_type="1x2", market_name="Победа хозяев", selection="home",
        bookmaker_odds=1.9, model_probability=0.55, confidence_score=65.0,
        confidence_level="средняя уверенность", recommendation_group="main",
        explanation="test", data_provider="mock", model_version="v1",
    )
    base.update(overrides)
    return Prediction(**base)


def test_settlement_cycle_settles_finished_events():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        storage = TrackingStorage(str(tmp_dir / "db.sqlite3"))
        pid = storage.save_prediction(_prediction())
        provider = MockResultProvider({
            "evt-past": EventResult(event_id="evt-past", status="finished", home_goals=2, away_goals=0)
        })
        report = run_settlement_cycle(storage, provider)
        row = storage.get_prediction(pid)
        storage.close()
        check("settlement cycle settles the finished event", row["status"] == STATUS_WON)
        check("report counts one settlement", report.settled == 1, report.settled)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_settlement_cycle_skips_events_without_a_result():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        storage = TrackingStorage(str(tmp_dir / "db.sqlite3"))
        pid = storage.save_prediction(_prediction(event_id="evt-no-result"))
        provider = MockResultProvider({})  # no data available
        report = run_settlement_cycle(storage, provider)
        row = storage.get_prediction(pid)
        storage.close()
        check("prediction stays pending when no result is available", row["status"] == STATUS_PENDING)
        check("report counts the skip", report.skipped_no_result == 1)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_settlement_cycle_skips_events_not_yet_started():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        storage = TrackingStorage(str(tmp_dir / "db.sqlite3"))
        pid = storage.save_prediction(_prediction(
            event_id="evt-future", event_start_time="2099-01-01T00:00:00+00:00"
        ))
        provider = MockResultProvider({
            "evt-future": EventResult(event_id="evt-future", status="finished", home_goals=1, away_goals=0)
        })
        report = run_settlement_cycle(storage, provider)
        row = storage.get_prediction(pid)
        storage.close()
        check("future event is not checked at all", row["status"] == STATUS_PENDING)
        check("report shows nothing checked", report.checked == 0)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_repeated_settlement_cycle_does_not_duplicate():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        storage = TrackingStorage(str(tmp_dir / "db.sqlite3"))
        pid = storage.save_prediction(_prediction(event_id="evt-repeat"))
        provider = MockResultProvider({
            "evt-repeat": EventResult(event_id="evt-repeat", status="finished", home_goals=1, away_goals=0)
        })
        first = run_settlement_cycle(storage, provider)
        second = run_settlement_cycle(storage, provider)
        history = storage.get_settlement_history(pid)
        storage.close()
        check("first cycle settles the prediction", first.settled == 1)
        check("second cycle finds nothing pending left to settle", second.checked == 0, second.checked)
        check("settlement history has exactly one record, no duplicates", len(history) == 1, len(history))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run():
    test_settlement_cycle_settles_finished_events()
    test_settlement_cycle_skips_events_without_a_result()
    test_settlement_cycle_skips_events_not_yet_started()
    test_repeated_settlement_cycle_does_not_duplicate()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
