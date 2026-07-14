"""
CSV/Excel export of the complete permanent prediction history for the AI
Betting Analytics module. Read-only against analytics/storage.py -- never
modifies predictions/results.
"""

from __future__ import annotations

import csv
from typing import List

from analytics.storage import AnalyticsStorage

EXPORT_FIELDNAMES = [
    "id", "created_at", "match_datetime", "sport", "country", "league", "fixture_id",
    "home_team", "away_team", "market", "market_label", "selection", "odds",
    "estimated_probability", "signal_level", "reason", "model_version", "archive_version",
    "prediction_source", "result_status", "final_home_score", "final_away_score",
    "won", "lost", "void", "profit", "stake", "checked_at",
]


def _rows_as_dicts(storage: AnalyticsStorage) -> List[dict]:
    return [{field: row[field] for field in EXPORT_FIELDNAMES} for row in storage.all_predictions_with_results()]


def export_csv(storage: AnalyticsStorage, path: str) -> str:
    rows = _rows_as_dicts(storage)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=EXPORT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    return path


def export_excel(storage: AnalyticsStorage, path: str) -> str:
    import pandas as pd  # local import: keep the pandas dependency optional at module load time

    rows = _rows_as_dicts(storage)
    df = pd.DataFrame(rows, columns=EXPORT_FIELDNAMES)
    df.to_excel(path, index=False, engine="openpyxl")
    return path
