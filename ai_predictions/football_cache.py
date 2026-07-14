"""
Quota-aware, 24h persistent cache for API-Football calls made by the
enrichment step (ai_predictions/enrichment.py). Two concerns live here,
both required to protect the tiny free-plan daily quota (100 requests/day):

1. A real 24h cache: the same real query (team resolution, a team's
   recent-form stats, ...) is never re-fetched from the network within
   its TTL, even across separate bot restarts (persisted to SQLite, not
   an in-process dict).
2. A persistent daily request counter with a hard reserve: once today's
   usage reaches (API_FOOTBALL_DAILY_QUOTA - API_FOOTBALL_QUOTA_RESERVE),
   no further real request is allowed for the rest of that day, no matter
   how many processes/restarts happen.

Never caches a "no answer yet" state as if it were a confirmed empty
result -- callers only call `set()` with data they already decided is
final (matches the same rule football/providers/api_football.py follows
for its own per-run caches).
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import threading
from typing import Any, Optional

from ai_predictions.value_config import (
    API_FOOTBALL_CACHE_TTL_HOURS,
    API_FOOTBALL_DAILY_QUOTA,
    API_FOOTBALL_QUOTA_RESERVE,
)

DEFAULT_DB_PATH = os.path.join("data", "api_football_cache.db")


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class FootballCache:
    def __init__(self, db_path: str = DEFAULT_DB_PATH, *, now: Optional[datetime.datetime] = None):
        self.db_path = db_path
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._now = now or _utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    cache_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    cached_at TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS quota_usage (
                    usage_date TEXT PRIMARY KEY,
                    requests_used INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    def close(self) -> None:
        self._conn.close()

    # -- 24h cache -----------------------------------------------------------

    def get(self, cache_key: str, *, ttl_hours: Optional[float] = None) -> Optional[Any]:
        """Returns the cached payload if it exists and is within the TTL,
        else None (a miss -- expired entries are never returned, but are
        left in place; they get overwritten by the next real `set`).
        `ttl_hours` overrides the default API_FOOTBALL_CACHE_TTL_HOURS for
        callers with their own explicit freshness requirement (e.g. the 6h
        fixture-list cache -- see value_config.FIXTURE_LIST_CACHE_TTL_HOURS)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json, cached_at FROM cache_entries WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        payload_json, cached_at = row
        try:
            cached_dt = datetime.datetime.fromisoformat(cached_at)
        except ValueError:
            return None
        ttl = API_FOOTBALL_CACHE_TTL_HOURS if ttl_hours is None else ttl_hours
        if self._now - cached_dt > datetime.timedelta(hours=ttl):
            return None
        return json.loads(payload_json)

    def cached_at(self, cache_key: str) -> Optional[datetime.datetime]:
        """Raw cache timestamp for diagnostics (e.g. /status "fixture
        cache age") -- ignores TTL entirely, unlike `get()`, so a caller
        can report "this entry is N hours old" even once expired."""
        with self._lock:
            row = self._conn.execute(
                "SELECT cached_at FROM cache_entries WHERE cache_key = ?", (cache_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            return datetime.datetime.fromisoformat(row[0])
        except ValueError:
            return None

    def set(self, cache_key: str, payload: Any) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO cache_entries (cache_key, payload_json, cached_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload_json = excluded.payload_json, cached_at = excluded.cached_at
                """,
                (cache_key, json.dumps(payload), self._now.isoformat()),
            )

    # -- daily quota -----------------------------------------------------------

    def _today_key(self) -> str:
        return self._now.date().isoformat()

    def requests_used_today(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT requests_used FROM quota_usage WHERE usage_date = ?", (self._today_key(),)
            ).fetchone()
        return row[0] if row else 0

    def requests_available(self) -> int:
        """How many more real requests may be spent today without eating
        into the reserve. Never negative."""
        used = self.requests_used_today()
        budget = API_FOOTBALL_DAILY_QUOTA - API_FOOTBALL_QUOTA_RESERVE
        return max(0, budget - used)

    def can_spend(self, count: int = 1) -> bool:
        return self.requests_available() >= count

    def record_requests(self, count: int) -> None:
        if count <= 0:
            return
        today = self._today_key()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO quota_usage (usage_date, requests_used) VALUES (?, ?)
                ON CONFLICT(usage_date) DO UPDATE SET requests_used = requests_used + excluded.requests_used
                """,
                (today, count),
            )
