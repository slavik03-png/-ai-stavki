"""
Persistence and duplicate-prevention tests for tracking/storage.py.

Uses a temporary SQLite file (never the real data/ai_stavki.db) so tests
never touch production data.
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from tracking.models import Prediction, EventResult, STATUS_WON
from tracking.storage import TrackingStorage, DuplicatePredictionError, row_to_event_result

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def _tmp_db_path(tmp_dir: Path) -> str:
    return str(tmp_dir / "test_ai_stavki.db")


def _sample_prediction(**overrides) -> Prediction:
    base = dict(
        sport="football", country="England", league="Premier League",
        event_id="evt-1", event_start_time="2026-07-10T18:00:00+00:00",
        home_team="Arsenal", away_team="Chelsea",
        market_type="1x2", market_name="Победа хозяев", selection="home",
        bookmaker_odds=1.85, model_probability=0.58, confidence_score=68.0,
        confidence_level="средняя уверенность", recommendation_group="main",
        explanation="форма хозяев дома выше среднего", data_provider="mock",
        model_version="v1",
    )
    base.update(overrides)
    return Prediction(**base)


def test_database_persists_after_reopening():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        db_path = _tmp_db_path(tmp_dir)
        storage = TrackingStorage(db_path)
        pid = storage.save_prediction(_sample_prediction())
        storage.close()

        reopened = TrackingStorage(db_path)
        row = reopened.get_prediction(pid)
        reopened.close()
        check("prediction survives close+reopen", row is not None and row["event_id"] == "evt-1")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_duplicate_prediction_is_rejected():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        storage = TrackingStorage(_tmp_db_path(tmp_dir))
        storage.save_prediction(_sample_prediction())
        raised = False
        try:
            storage.save_prediction(_sample_prediction())  # identical dedup_key
        except DuplicatePredictionError:
            raised = True
        storage.close()
        check("duplicate event/market/selection/line/model_version is rejected", raised)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_different_selection_is_not_a_duplicate():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        storage = TrackingStorage(_tmp_db_path(tmp_dir))
        storage.save_prediction(_sample_prediction(selection="home"))
        raised = False
        try:
            storage.save_prediction(_sample_prediction(selection="away", event_id="evt-1"))
        except DuplicatePredictionError:
            raised = True
        storage.close()
        check("different selection on same event is allowed (not a duplicate)", not raised)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_settlement_updates_status_once():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        storage = TrackingStorage(_tmp_db_path(tmp_dir))
        pid = storage.save_prediction(_sample_prediction())
        applied_first = storage.update_prediction_settlement(
            pid, STATUS_WON, "2:1", "1:0", "победа хозяев подтверждена"
        )
        applied_second = storage.update_prediction_settlement(
            pid, STATUS_WON, "2:1", "1:0", "повторная попытка settlement"
        )
        row = storage.get_prediction(pid)
        history = storage.get_settlement_history(pid)
        storage.close()
        check("first settlement is applied", applied_first)
        check("second settlement on already-settled prediction is a no-op", not applied_second)
        check("status reflects the settlement", row["status"] == STATUS_WON)
        check("settlement_history has exactly one entry", len(history) == 1, len(history))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_event_result_roundtrip():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        storage = TrackingStorage(_tmp_db_path(tmp_dir))
        result = EventResult(event_id="evt-9", status="finished", home_goals=2, away_goals=1,
                              ht_home_goals=1, ht_away_goals=0)
        storage.save_event_result(result)
        row = storage.get_event_result("evt-9")
        restored = row_to_event_result(row)
        storage.close()
        check("event result roundtrips through storage", restored.home_goals == 2 and restored.away_goals == 1)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_never_deletes_existing_records():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        db_path = _tmp_db_path(tmp_dir)
        storage = TrackingStorage(db_path)
        storage.save_prediction(_sample_prediction())
        storage.close()

        # Reopening (simulating a restart) must not wipe existing rows.
        reopened = TrackingStorage(db_path)
        all_rows = reopened.list_all_predictions()
        reopened.close()
        check("reopening keeps existing predictions intact", len(all_rows) == 1, len(all_rows))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run():
    test_database_persists_after_reopening()
    test_duplicate_prediction_is_rejected()
    test_different_selection_is_not_a_duplicate()
    test_settlement_updates_status_once()
    test_event_result_roundtrip()
    test_never_deletes_existing_records()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
