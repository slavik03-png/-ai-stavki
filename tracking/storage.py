"""
Persistent SQLite storage for the tracking package.

Database file defaults to `data/ai_stavki.db` and survives bot/workflow
restarts. Tables are created with `CREATE TABLE IF NOT EXISTS` -- no
destructive migrations, no automatic deletion of existing records.

Uniqueness is enforced at the database level via a `dedup_key` UNIQUE
column, so the same event/market/selection/line/model_version combination
can never be stored twice even under concurrent access.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
import threading
from dataclasses import asdict
from typing import Iterable, List, Optional

from tracking.models import (
    EventResult,
    Prediction,
    STATUS_PENDING,
)

DEFAULT_DB_PATH = os.path.join("data", "ai_stavki.db")


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class DuplicatePredictionError(Exception):
    """Raised when a prediction with the same dedup_key already exists."""


class TrackingStorage:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._create_tables()

    # -- schema ------------------------------------------------------------

    def _create_tables(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS predictions (
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
                    settlement_explanation TEXT,
                    signal_level TEXT,
                    ranking_score REAL,
                    outlier_warning INTEGER NOT NULL DEFAULT 0,
                    rejection_reason TEXT
                )
                """
            )
            self._add_missing_columns()
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS event_results (
                    event_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    home_goals INTEGER,
                    away_goals INTEGER,
                    ht_home_goals INTEGER,
                    ht_away_goals INTEGER,
                    home_corners INTEGER,
                    away_corners INTEGER,
                    home_cards INTEGER,
                    away_cards INTEGER,
                    home_fouls INTEGER,
                    away_fouls INTEGER,
                    home_shots INTEGER,
                    away_shots INTEGER,
                    retrieved_at TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settlement_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prediction_id TEXT NOT NULL,
                    previous_status TEXT NOT NULL,
                    new_status TEXT NOT NULL,
                    explanation TEXT,
                    settled_at TEXT NOT NULL,
                    FOREIGN KEY (prediction_id) REFERENCES predictions(prediction_id)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_predictions_status ON predictions(status)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_predictions_event ON predictions(event_id)"
            )

    def _add_missing_columns(self) -> None:
        """Additive-only migration for databases created before the ranked
        HIGH/MEDIUM/LOW/REJECTED signal system existed: adds any missing
        columns with ALTER TABLE ADD COLUMN. Never drops or renames a
        column, never touches existing rows -- old rows simply read back
        with NULL/0 defaults for the new fields."""
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(predictions)").fetchall()}
        needed = {
            "signal_level": "TEXT",
            "ranking_score": "REAL",
            "outlier_warning": "INTEGER NOT NULL DEFAULT 0",
            "rejection_reason": "TEXT",
        }
        for column, coltype in needed.items():
            if column not in existing:
                self._conn.execute(f"ALTER TABLE predictions ADD COLUMN {column} {coltype}")

    def close(self) -> None:
        self._conn.close()

    # -- predictions ---------------------------------------------------------

    def save_prediction(self, prediction: Prediction) -> str:
        """Inserts a new prediction. Raises DuplicatePredictionError if the
        same event/market/selection/line/model_version was already saved."""
        if prediction.created_at is None:
            prediction.created_at = utc_now_iso()
        row = asdict(prediction)
        row["dedup_key"] = prediction.dedup_key
        row["outlier_warning"] = 1 if prediction.outlier_warning else 0
        columns = [
            "prediction_id", "dedup_key", "created_at", "sport", "country", "league",
            "event_id", "event_start_time", "home_team", "away_team", "market_type",
            "market_name", "selection", "line", "bookmaker_odds", "model_probability",
            "confidence_score", "confidence_level", "recommendation_group", "explanation",
            "data_provider", "model_version", "status", "final_score", "first_half_score",
            "settled_at", "settlement_explanation",
            "signal_level", "ranking_score", "outlier_warning", "rejection_reason",
        ]
        placeholders = ", ".join(f":{c}" for c in columns)
        sql = f"INSERT INTO predictions ({', '.join(columns)}) VALUES ({placeholders})"
        try:
            with self._lock, self._conn:
                self._conn.execute(sql, row)
        except sqlite3.IntegrityError as exc:
            raise DuplicatePredictionError(
                f"prediction already exists for dedup_key={row['dedup_key']!r}"
            ) from exc
        return prediction.prediction_id

    def get_prediction(self, prediction_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM predictions WHERE prediction_id = ?", (prediction_id,)
            )
            return cur.fetchone()

    def get_pending_predictions(self, sport: Optional[str] = None) -> List[sqlite3.Row]:
        with self._lock:
            if sport:
                cur = self._conn.execute(
                    "SELECT * FROM predictions WHERE status = ? AND sport = ? ORDER BY event_start_time",
                    (STATUS_PENDING, sport),
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM predictions WHERE status = ? ORDER BY event_start_time",
                    (STATUS_PENDING,),
                )
            return cur.fetchall()

    def list_predictions(
        self,
        sport: Optional[str] = None,
        league: Optional[str] = None,
        market_type: Optional[str] = None,
        recommendation_group: Optional[str] = None,
        status: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
    ) -> List[sqlite3.Row]:
        clauses = []
        params: list = []
        if sport:
            clauses.append("sport = ?")
            params.append(sport)
        if league:
            clauses.append("league = ?")
            params.append(league)
        if market_type:
            clauses.append("market_type = ?")
            params.append(market_type)
        if recommendation_group:
            clauses.append("recommendation_group = ?")
            params.append(recommendation_group)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if created_after:
            clauses.append("created_at >= ?")
            params.append(created_after)
        if created_before:
            clauses.append("created_at <= ?")
            params.append(created_before)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM predictions {where} ORDER BY created_at"
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    def list_all_predictions(self) -> List[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM predictions ORDER BY created_at")
            return cur.fetchall()

    def update_prediction_settlement(
        self,
        prediction_id: str,
        status: str,
        final_score: Optional[str],
        first_half_score: Optional[str],
        settlement_explanation: str,
        settled_at: Optional[str] = None,
    ) -> bool:
        """Updates a prediction's settlement outcome. Returns False (no-op)
        if the prediction is not currently pending -- this is what prevents
        the same prediction from being settled twice."""
        settled_at = settled_at or utc_now_iso()
        with self._lock, self._conn:
            current = self._conn.execute(
                "SELECT status FROM predictions WHERE prediction_id = ?", (prediction_id,)
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown prediction_id {prediction_id!r}")
            previous_status = current["status"]
            if previous_status != STATUS_PENDING:
                return False
            self._conn.execute(
                """
                UPDATE predictions
                SET status = ?, final_score = ?, first_half_score = ?,
                    settled_at = ?, settlement_explanation = ?
                WHERE prediction_id = ?
                """,
                (status, final_score, first_half_score, settled_at,
                 settlement_explanation, prediction_id),
            )
            self._conn.execute(
                """
                INSERT INTO settlement_history
                    (prediction_id, previous_status, new_status, explanation, settled_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (prediction_id, previous_status, status, settlement_explanation, settled_at),
            )
        return True

    def get_settlement_history(self, prediction_id: str) -> List[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM settlement_history WHERE prediction_id = ? ORDER BY id",
                (prediction_id,),
            )
            return cur.fetchall()

    # -- event results ---------------------------------------------------------

    def save_event_result(self, result: EventResult) -> None:
        result_retrieved_at = result.retrieved_at or utc_now_iso()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO event_results (
                    event_id, status, home_goals, away_goals, ht_home_goals, ht_away_goals,
                    home_corners, away_corners, home_cards, away_cards,
                    home_fouls, away_fouls, home_shots, away_shots, retrieved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    status=excluded.status, home_goals=excluded.home_goals,
                    away_goals=excluded.away_goals, ht_home_goals=excluded.ht_home_goals,
                    ht_away_goals=excluded.ht_away_goals, home_corners=excluded.home_corners,
                    away_corners=excluded.away_corners, home_cards=excluded.home_cards,
                    away_cards=excluded.away_cards, home_fouls=excluded.home_fouls,
                    away_fouls=excluded.away_fouls, home_shots=excluded.home_shots,
                    away_shots=excluded.away_shots, retrieved_at=excluded.retrieved_at
                """,
                (
                    result.event_id, result.status, result.home_goals, result.away_goals,
                    result.ht_home_goals, result.ht_away_goals, result.home_corners,
                    result.away_corners, result.home_cards, result.away_cards,
                    result.home_fouls, result.away_fouls, result.home_shots,
                    result.away_shots, result_retrieved_at,
                ),
            )

    def get_event_result(self, event_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM event_results WHERE event_id = ?", (event_id,)
            )
            return cur.fetchone()


def row_to_event_result(row: sqlite3.Row) -> EventResult:
    return EventResult(
        event_id=row["event_id"],
        status=row["status"],
        home_goals=row["home_goals"],
        away_goals=row["away_goals"],
        ht_home_goals=row["ht_home_goals"],
        ht_away_goals=row["ht_away_goals"],
        home_corners=row["home_corners"],
        away_corners=row["away_corners"],
        home_cards=row["home_cards"],
        away_cards=row["away_cards"],
        home_fouls=row["home_fouls"],
        away_fouls=row["away_fouls"],
        home_shots=row["home_shots"],
        away_shots=row["away_shots"],
        retrieved_at=row["retrieved_at"],
    )
