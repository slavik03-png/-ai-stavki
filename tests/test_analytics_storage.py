"""
Storage/aggregation tests for analytics/storage.py -- no network calls, no
real API keys. Uses a temporary SQLite file, never the real
data/analytics.db.
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from analytics.storage import AnalyticsStorage

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def _tmp_db(tmp_dir: Path) -> str:
    return str(tmp_dir / "test_analytics.db")


def _sample_prediction(**overrides):
    base = dict(
        match_datetime="2026-07-10T18:00:00+00:00", sport="football", country="England",
        league="Premier League", fixture_id=1001, home_team="Arsenal", away_team="Chelsea",
        market="home_win", market_label="Победа хозяев", selection="home_win", odds=1.85,
        estimated_probability=0.58, signal_level="HIGH", reason="форма хозяев дома выше среднего",
        model_version="football-predictions-v3", archive_version="2026-07-10",
        prediction_source="api_football",
    )
    base.update(overrides)
    return base


def test_prediction_insertion_and_duplicate_protection():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        storage = AnalyticsStorage(_tmp_db(tmp_dir))
        pid = storage.record_prediction(_sample_prediction())
        check("first insert returns an id", pid is not None)

        dup_id = storage.record_prediction(_sample_prediction(reason="different text, same fixture+market"))
        check("duplicate (same fixture_id+market) is rejected", dup_id is None)

        other_market = storage.record_prediction(_sample_prediction(market="away_win"))
        check("different market on same fixture is a new row", other_market is not None and other_market != pid)

        row = storage.get_prediction(pid)
        check("stored row round-trips home_team", row is not None and row["home_team"] == "Arsenal")
        storage.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_result_recording_is_append_only():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        storage = AnalyticsStorage(_tmp_db(tmp_dir))
        pid = storage.record_prediction(_sample_prediction())

        wrote_first = storage.record_result(
            prediction_id=pid, fixture_id=1001, final_home_score=2, final_away_score=0,
            status="won", won=True, lost=False, void=False, profit=85.0, stake=100.0,
        )
        check("first result write succeeds", wrote_first is True)

        wrote_second = storage.record_result(
            prediction_id=pid, fixture_id=1001, final_home_score=0, final_away_score=0,
            status="lost", won=False, lost=True, void=False, profit=-100.0, stake=100.0,
        )
        check("second write to the same prediction is rejected (append-only)", wrote_second is False)

        pending = storage.pending_predictions()
        check("settled prediction no longer pending", all(r["id"] != pid for r in pending))
        storage.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_roi_and_aggregation():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        storage = AnalyticsStorage(_tmp_db(tmp_dir))
        # Two winning bets at odds 2.0 (profit +100 each), one losing bet
        # (profit -100) -- stake 100. Decisive win rate: 2/3 = 66.67%.
        # ROI on 3 settled bets, stake 100: (100+100-100) / (100*3) * 100 = 33.33%
        p1 = storage.record_prediction(_sample_prediction(fixture_id=1, market="home_win", odds=2.0, league="Premier League"))
        p2 = storage.record_prediction(_sample_prediction(fixture_id=2, market="home_win", odds=2.0, league="La Liga"))
        p3 = storage.record_prediction(_sample_prediction(fixture_id=3, market="away_win", odds=2.0, league="Premier League"))
        storage.record_result(prediction_id=p1, fixture_id=1, final_home_score=2, final_away_score=0,
                               status="won", won=True, lost=False, void=False, profit=100.0, stake=100.0)
        storage.record_result(prediction_id=p2, fixture_id=2, final_home_score=2, final_away_score=0,
                               status="won", won=True, lost=False, void=False, profit=100.0, stake=100.0)
        storage.record_result(prediction_id=p3, fixture_id=3, final_home_score=0, final_away_score=2,
                               status="lost", won=False, lost=True, void=False, profit=-100.0, stake=100.0)

        overall = storage.overall_stats(stake=100.0)
        check("win_rate is 66.67%", abs(overall["win_rate"] - 66.67) < 0.01, overall["win_rate"])
        check("roi is 33.33%", abs(overall["roi"] - 33.33) < 0.01, overall["roi"])
        check("profit is 100.0", abs(overall["profit"] - 100.0) < 0.01, overall["profit"])

        by_league = {g["key"]: g for g in storage.group_stats("league", stake=100.0)}
        check("Premier League has 2 settled (1 win, 1 loss)",
              by_league["Premier League"]["settled_predictions"] == 2 and by_league["Premier League"]["wins"] == 1)
        check("La Liga has 1 settled win", by_league["La Liga"]["wins"] == 1 and by_league["La Liga"]["losses"] == 0)

        by_market = {g["key"]: g for g in storage.group_stats("market", stake=100.0)}
        check("home_win market has 2 settled", by_market["home_win"]["settled_predictions"] == 2)

        storage.refresh_statistics(stake=100.0)
        with storage._conn:
            row = storage._conn.execute(
                "SELECT * FROM league_statistics WHERE league = ?", ("Premier League",)
            ).fetchone()
        check("materialized league_statistics table has the same win count", row is not None and row["wins"] == 1)
        storage.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run():
    test_prediction_insertion_and_duplicate_protection()
    test_result_recording_is_append_only()
    test_roi_and_aggregation()
    failed = [name for name, status in results if status == "FAIL"]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


if __name__ == "__main__":
    run()
