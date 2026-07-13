"""
Tests for the ranked HIGH/MEDIUM/LOW/REJECTED signal fields added to
tracking/models.py, tracking/storage.py and tracking/statistics.py:
- Prediction validates signal_level against the fixed set (or None);
- storage persists/reads the new columns and migrates old databases
  additively (no data loss);
- statistics.by_signal_level groups correctly;
- statistics.sample_size_note gives the documented 3-tier wording.
"""

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, ".")

from tracking import statistics as stats_mod
from tracking.models import Prediction, STATUS_PENDING, STATUS_WON
from tracking.statistics import (
    PRELIMINARY_SAMPLE_MAX,
    VERY_SMALL_SAMPLE_MAX,
    sample_size_note,
)
from tracking.storage import TrackingStorage

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def _prediction(**overrides):
    base = dict(
        sport="football", country=None, league="Premier League",
        event_id="evt-1", event_start_time="2026-07-13T12:00:00Z",
        home_team="Home FC", away_team="Away FC", market_type="1x2",
        market_name="Исход матча (1X2)", selection="Home FC",
        bookmaker_odds=2.2, model_probability=0.5, confidence_score=5.0,
        confidence_level="среднее согласие рынка", recommendation_group="main",
        explanation="test", data_provider="the_odds_api", model_version="value-ranking-v2.0",
    )
    base.update(overrides)
    return Prediction(**base)


def test_prediction_accepts_valid_signal_level():
    p = _prediction(signal_level="HIGH", ranking_score=12.5, outlier_warning=False, rejection_reason=None)
    check("Prediction accepts a valid ranked signal level", p.signal_level == "HIGH")


def test_prediction_accepts_none_signal_level_for_legacy_rows():
    p = _prediction(signal_level=None)
    check("Prediction still accepts signal_level=None (legacy statistics-based rows)", p.signal_level is None)


def test_prediction_rejects_invalid_signal_level():
    try:
        _prediction(signal_level="ULTRA")
        check("Prediction rejects an invalid signal_level", False)
    except ValueError:
        check("Prediction rejects an invalid signal_level", True)


def test_storage_persists_and_reads_back_new_columns():
    with tempfile.TemporaryDirectory() as d:
        storage = TrackingStorage(db_path=os.path.join(d, "t.db"))
        p = _prediction(signal_level="MEDIUM", ranking_score=3.14, outlier_warning=True,
                         rejection_reason="Понижено с HIGH до MEDIUM из-за предупреждения о выбросе в цене")
        storage.save_prediction(p)
        rows = storage.list_all_predictions()
        check("exactly one row persisted", len(rows) == 1)
        row = rows[0]
        check("signal_level round-trips", row["signal_level"] == "MEDIUM")
        check("ranking_score round-trips", abs(row["ranking_score"] - 3.14) < 1e-9)
        check("outlier_warning round-trips as truthy int", bool(row["outlier_warning"]) is True)
        check("rejection_reason round-trips", "выброс" in row["rejection_reason"])
        storage.close()


def test_storage_migrates_pre_existing_database_additively():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "old.db")
        # Simulate a database created before the ranked-signal columns
        # existed: create the predictions table WITHOUT them, insert a
        # real legacy row, then open it with the current TrackingStorage
        # and confirm the row survives and new columns are usable.
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE predictions (
                prediction_id TEXT PRIMARY KEY,
                dedup_key TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                sport TEXT NOT NULL,
                country TEXT,
                league TEXT,
                event_id TEXT NOT NULL,
                event_start_time TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                market_type TEXT NOT NULL,
                market_name TEXT NOT NULL,
                selection TEXT NOT NULL,
                line REAL,
                bookmaker_odds REAL NOT NULL,
                model_probability REAL NOT NULL,
                confidence_score REAL NOT NULL,
                confidence_level TEXT NOT NULL,
                recommendation_group TEXT NOT NULL,
                explanation TEXT NOT NULL,
                data_provider TEXT NOT NULL,
                model_version TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                final_score TEXT,
                first_half_score TEXT,
                settled_at TEXT,
                settlement_explanation TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO predictions (prediction_id, dedup_key, created_at, sport, country, league, "
            "event_id, event_start_time, home_team, away_team, market_type, market_name, selection, "
            "line, bookmaker_odds, model_probability, confidence_score, confidence_level, "
            "recommendation_group, explanation, data_provider, model_version, status) VALUES "
            "('pred-old', 'evt-old|1x2|Home FC|none|v1', '2026-01-01T00:00:00+00:00', 'football', NULL, "
            "'Premier League', 'evt-old', '2026-01-02T00:00:00Z', 'Home FC', 'Away FC', '1x2', "
            "'Исход матча (1X2)', 'Home FC', NULL, 2.0, 0.5, 60.0, 'средняя уверенность', 'main', "
            "'legacy row', 'api-football', 'v1', 'won')"
        )
        conn.commit()
        conn.close()

        storage = TrackingStorage(db_path=db_path)
        rows = storage.list_all_predictions()
        check("pre-existing legacy row survives the migration", len(rows) == 1)
        check("legacy row's real data is untouched", rows[0]["home_team"] == "Home FC" and rows[0]["status"] == "won")
        check("legacy row reads back signal_level as NULL, not an error", rows[0]["signal_level"] is None)

        # And a new ranked-system row can now be saved into the same,
        # migrated database.
        p = _prediction(event_id="evt-new", signal_level="LOW")
        storage.save_prediction(p)
        rows2 = storage.list_all_predictions()
        check("a new ranked-signal row can be saved after migration", len(rows2) == 2)
        storage.close()


def test_by_signal_level_groups_correctly():
    class FakeRow(dict):
        def __getitem__(self, key):
            return dict.__getitem__(self, key)

    def row(level, status, odds=2.0):
        return FakeRow(status=status, bookmaker_odds=odds, signal_level=level,
                        created_at="2026-07-01T00:00:00+00:00")

    rows = [
        row("HIGH", "won"), row("HIGH", "lost"),
        row("MEDIUM", "won"),
        row("REJECTED", "lost"),
        row(None, "won"),  # legacy row with no ranked level
    ]
    grouped = stats_mod.by_signal_level(rows)
    check("HIGH bucket has 2 settled predictions", grouped["HIGH"].settled == 2, grouped["HIGH"].settled)
    check("MEDIUM bucket has 1 settled prediction", grouped["MEDIUM"].settled == 1)
    check("REJECTED candidates are tracked too (Step 7 measurability)", grouped["REJECTED"].settled == 1)
    # group_by's convention (shared by every other breakdown) folds a
    # missing/falsy key into the "не указано" bucket, not a Python None
    # key -- match that existing convention rather than inventing a new one.
    check("legacy rows with no signal_level group under the shared 'не указано' bucket",
          "не указано" in grouped and grouped["не указано"].settled == 1, list(grouped.keys()))


def test_sample_size_note_three_tiers():
    very_small = sample_size_note(VERY_SMALL_SAMPLE_MAX)
    preliminary_low = sample_size_note(VERY_SMALL_SAMPLE_MAX + 1)
    preliminary_high = sample_size_note(PRELIMINARY_SAMPLE_MAX)
    meaningful = sample_size_note(PRELIMINARY_SAMPLE_MAX + 1)
    check("<=29 settled is 'very small sample'", "Очень маленькая" in very_small, very_small)
    check("30 settled crosses into 'preliminary'", "Предварительная" in preliminary_low, preliminary_low)
    check("99 settled is still 'preliminary'", "Предварительная" in preliminary_high, preliminary_high)
    check("100 settled becomes 'meaningful but not conclusive'", "Значимая" in meaningful, meaningful)
    check("even a meaningful sample still disclaims future profit",
          "не гарантир" in meaningful.lower(), meaningful)


def run():
    test_prediction_accepts_valid_signal_level()
    test_prediction_accepts_none_signal_level_for_legacy_rows()
    test_prediction_rejects_invalid_signal_level()
    test_storage_persists_and_reads_back_new_columns()
    test_storage_migrates_pre_existing_database_additively()
    test_by_signal_level_groups_correctly()
    test_sample_size_note_three_tiers()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
