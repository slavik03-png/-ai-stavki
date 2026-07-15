"""
Tests for the pre_match/live mode marker (Task #11) across
tracking/models.py, tracking/storage.py, analytics/storage.py and
analytics/reports.py's compact_report split.
"""

import datetime
import os
import sys

sys.path.insert(0, ".")

from analytics.integration import record_recommendation
from analytics.reports import compact_report
from analytics.storage import AnalyticsStorage
from tracking.models import MODE_LIVE, MODE_PRE_MATCH, Prediction, STATUS_PENDING, STATUS_WON
from tracking.storage import DuplicatePredictionError, TrackingStorage

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


NOW = datetime.datetime(2026, 7, 15, 12, 0, tzinfo=datetime.timezone.utc)


def _prediction(mode, event_id="api_football:1", **overrides):
    kwargs = dict(
        sport="football", country="Testland", league="Test League", event_id=event_id,
        event_start_time="2026-07-15T11:00:00+00:00", home_team="Home FC", away_team="Away FC",
        market_type="h2h", market_name="1X2", selection="Home FC", bookmaker_odds=2.10,
        model_probability=0.55, confidence_score=55.0, confidence_level="high",
        recommendation_group="main", explanation="test", data_provider="test",
        model_version="test-v1", mode=mode,
    )
    kwargs.update(overrides)
    return Prediction(**kwargs)


def test_invalid_mode_is_rejected():
    raised = False
    try:
        _prediction("something_else")
    except ValueError:
        raised = True
    check("Prediction rejects an unknown mode value", raised)


def test_default_mode_is_pre_match():
    p = _prediction(MODE_PRE_MATCH)
    check("default/explicit pre_match mode round-trips", p.mode == MODE_PRE_MATCH)


def test_same_fixture_market_different_mode_does_not_collide():
    """A live pick and a pre-match pick on the EXACT same fixture/market
    must both be stored -- the mode marker keeps their dedup keys apart."""
    db_path = "/tmp/test_mode_marker_tracking.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    storage = TrackingStorage(db_path=db_path)
    try:
        pre_match = _prediction(MODE_PRE_MATCH, event_id="api_football:100")
        live = _prediction(MODE_LIVE, event_id="api_football:100")
        check("dedup keys differ by mode alone", pre_match.dedup_key != live.dedup_key)

        storage.save_prediction(pre_match)
        storage.save_prediction(live)  # must NOT raise DuplicatePredictionError
        check("both a pre-match and a live pick for the same fixture/market are stored", True)

        raised = False
        try:
            storage.save_prediction(_prediction(MODE_LIVE, event_id="api_football:100"))
        except DuplicatePredictionError:
            raised = True
        check("saving the exact same live pick twice is still rejected as a duplicate", raised)
    finally:
        storage.close()


def test_analytics_dedup_key_also_folds_in_mode():
    db_path = "/tmp/test_mode_marker_analytics.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    storage = AnalyticsStorage(db_path=db_path, now=NOW)
    try:
        pre_match_row = {
            "match_datetime": NOW.isoformat(), "sport": "football", "fixture_id": 200,
            "home_team": "Home FC", "away_team": "Away FC", "market": "h2h",
            "selection": "Home FC", "odds": 2.0, "estimated_probability": 0.5,
            "signal_level": "HIGH", "model_version": "v1", "mode": "pre_match",
        }
        live_row = dict(pre_match_row, mode="live")
        storage.record_prediction(pre_match_row)
        storage.record_prediction(live_row)  # must not be treated as a duplicate of the pre-match row

        overall_all = storage.overall_stats()
        overall_pre_match = storage.overall_stats(mode="pre_match")
        overall_live = storage.overall_stats(mode="live")
        check("both rows are recorded (no false dedup across modes)", overall_all["total_predictions"] == 2)
        check("pre_match filter sees only the pre-match row", overall_pre_match["total_predictions"] == 1)
        check("live filter sees only the live row", overall_live["total_predictions"] == 1)
    finally:
        storage.close()


def test_compact_report_shows_three_separate_sections():
    db_path = "/tmp/test_mode_marker_report.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    storage = AnalyticsStorage(db_path=db_path, now=NOW)
    try:
        storage.record_prediction({
            "match_datetime": NOW.isoformat(), "sport": "football", "fixture_id": 301,
            "home_team": "A", "away_team": "B", "market": "h2h", "selection": "A",
            "odds": 2.0, "estimated_probability": 0.5, "signal_level": "HIGH",
            "model_version": "v1", "mode": "pre_match",
        })
        report = compact_report(storage, now=NOW)
        check("report has a combined section", "Всего (пред-матч + Live)" in report)
        check("report has a pre-match-only section", "Пред-матч" in report)
        check("report has a Live-only section", "🔴 Live" in report)
        check("Live section says 'no predictions yet' when empty, not zeros pretending to be real", "пока нет прогнозов" in report)
    finally:
        storage.close()


def run():
    test_invalid_mode_is_rejected()
    test_default_mode_is_pre_match()
    test_same_fixture_market_different_mode_does_not_collide()
    test_analytics_dedup_key_also_folds_in_mode()
    test_compact_report_shows_three_separate_sections()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
