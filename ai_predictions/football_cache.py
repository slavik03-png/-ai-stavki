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
            # Per-user shown-tracking (2026-07-15): which real picks from
            # the shared daily pool have already been shown to a SPECIFIC
            # Telegram user on a given Yekaterinburg calendar day, so a
            # later re-selection for that same user can exclude them and
            # surface the next-best remaining candidates instead of
            # repeating one already seen. Shared across all users (the
            # pool itself lives elsewhere, in the daily-archive cache
            # entry) -- this table only ever records "user X has seen
            # fixture Y / market Z today", nothing about the pool itself.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shown_picks (
                    local_date TEXT NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    fixture_id INTEGER NOT NULL,
                    market_key TEXT NOT NULL,
                    shown_at TEXT NOT NULL,
                    PRIMARY KEY (local_date, telegram_user_id, fixture_id, market_key)
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

    # -- per-user shown-tracking -----------------------------------------

    def get_shown_keys(self, local_date: str, telegram_user_id: int) -> set:
        """Returns the set of (fixture_id, market_key) pairs already shown
        to this Telegram user on this Yekaterinburg calendar date."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT fixture_id, market_key FROM shown_picks "
                "WHERE local_date = ? AND telegram_user_id = ?",
                (local_date, telegram_user_id),
            ).fetchall()
        return {(fixture_id, market_key) for fixture_id, market_key in rows}

    def mark_shown(self, local_date: str, telegram_user_id: int, entries) -> None:
        """Records each (fixture_id, market_key) pair in `entries` as shown
        to this user on this date. Idempotent -- a pick shown again (e.g.
        it is still eligible on a later press before this table is
        consulted) is never duplicated or double-recorded."""
        entries = list(entries)
        if not entries:
            return
        with self._lock, self._conn:
            for fixture_id, market_key in entries:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO shown_picks
                        (local_date, telegram_user_id, fixture_id, market_key, shown_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (local_date, telegram_user_id, fixture_id, market_key, self._now.isoformat()),
                )

    def clear_shown_for_user(self, local_date: str, telegram_user_id: int) -> int:
        """Deletes only this user's shown-history for this date (never the
        shared pool, tracking, or analytics data). Returns how many rows
        were cleared, for an honest confirmation reply."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM shown_picks WHERE local_date = ? AND telegram_user_id = ?",
                (local_date, telegram_user_id),
            )
            return cur.rowcount

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
