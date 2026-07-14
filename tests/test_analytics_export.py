"""
CSV/Excel export tests for analytics/export.py -- no network calls.
"""

import csv
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from analytics.export import export_csv, export_excel
from analytics.storage import AnalyticsStorage

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def _sample_prediction(**overrides):
    base = dict(
        match_datetime="2026-07-10T18:00:00+00:00", sport="football", country="England",
        league="Premier League", fixture_id=777, home_team="Arsenal", away_team="Chelsea",
        market="home_win", market_label="Победа хозяев", selection="home_win", odds=1.85,
        estimated_probability=0.58, signal_level="HIGH", reason="test", model_version="v3",
        archive_version="2026-07-10", prediction_source="api_football",
    )
    base.update(overrides)
    return base


def test_csv_export_contains_prediction_and_result():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        storage = AnalyticsStorage(str(tmp_dir / "analytics.db"))
        pid = storage.record_prediction(_sample_prediction())
        storage.record_result(prediction_id=pid, fixture_id=777, final_home_score=2, final_away_score=0,
                               status="won", won=True, lost=False, void=False, profit=85.0, stake=100.0)

        csv_path = str(tmp_dir / "export.csv")
        export_csv(storage, csv_path)
        with open(csv_path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        check("csv has exactly one row", len(rows) == 1, len(rows))
        check("csv row has the right fixture", rows[0]["fixture_id"] == "777")
        check("csv row has the settlement status", rows[0]["result_status"] == "won")
        storage.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_excel_export_round_trips():
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        storage = AnalyticsStorage(str(tmp_dir / "analytics.db"))
        storage.record_prediction(_sample_prediction())
        xlsx_path = str(tmp_dir / "export.xlsx")
        export_excel(storage, xlsx_path)
        check("xlsx file was created", Path(xlsx_path).exists())

        import pandas as pd
        df = pd.read_excel(xlsx_path, engine="openpyxl")
        check("xlsx has one row", len(df) == 1, len(df))
        check("xlsx row has the right home team", df.iloc[0]["home_team"] == "Arsenal")
        storage.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run():
    test_csv_export_contains_prediction_and_result()
    test_excel_export_round_trips()
    failed = [name for name, status in results if status == "FAIL"]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


if __name__ == "__main__":
    run()
