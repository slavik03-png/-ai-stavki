"""
Background result checker for the AI Betting Analytics module.

Finds analytics predictions whose match has had enough time to finish,
fetches the real final score from API-Football (through the existing,
unmodified FootballCache quota reserve -- never a second, competing
quota), and settles them with tracking.settlement.settle_prediction --
the same deterministic engine the rest of the project uses. Writes are
append-only: a prediction that already has a result row is never
re-checked or re-graded.

This module never touches tracking's own database, never modifies
football/providers/api_football.py, and never spends a request outside
FootballCache.can_spend()'s existing daily reserve.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, Optional

import requests

from analytics.config import (
    DEFAULT_STAKE,
    FINISHED_STATUSES,
    MARKET_KEY_MAP,
    RESULT_CHECK_MIN_HOURS_AFTER_KICKOFF,
    VOID_STATUSES,
)
from analytics.storage import AnalyticsStorage
from ai_predictions.football_cache import FootballCache
from tracking.models import EventResult, Prediction
from tracking.settlement import settle_prediction

logger = logging.getLogger(__name__)

BASE_URL = "https://v3.football.api-sports.io"

_STATUS_MAP_TO_PROFIT_FLAGS = {
    "won": (True, False, False),
    "half_won": (True, False, False),
    "lost": (False, True, False),
    "half_lost": (False, True, False),
    "returned": (False, False, True),
    "cancelled": (False, False, True),
    "postponed": (False, False, True),
    "void": (False, False, True),
    "unresolved": (False, False, False),
}


def _profit_for(status: str, odds: Optional[float], stake: float) -> float:
    odds = odds if odds is not None else 1.0
    if status == "won":
        return stake * (odds - 1.0)
    if status == "half_won":
        return 0.5 * stake * (odds - 1.0)
    if status == "lost":
        return -stake
    if status == "half_lost":
        return -0.5 * stake
    return 0.0


def fetch_fixture_result(
    fixture_id: int, api_key: Optional[str], football_cache: FootballCache, *, session: Any = requests,
) -> Optional[Dict[str, Any]]:
    """Returns a dict with status/home_goals/away_goals/ht_* once the real
    fixture result is known, or None if it is not known yet (not finished,
    quota exhausted this cycle, or a transient network/API error) -- never
    a fabricated value, and a "still in progress" answer is never cached,
    only a truly final one."""
    cache_key = f"analytics_fixture_result:{fixture_id}"
    cached = football_cache.get(cache_key, ttl_hours=24.0 * 365)
    if cached is not None:
        return cached

    if not api_key or not football_cache.can_spend(1):
        return None

    try:
        resp = session.get(
            f"{BASE_URL}/fixtures",
            params={"id": fixture_id},
            headers={"x-apisports-key": api_key},
            timeout=20,
        )
    except requests.RequestException as exc:
        logger.warning("analytics result checker: network error for fixture %s: %s", fixture_id, exc)
        return None
    football_cache.record_requests(1)

    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    response = payload.get("response") or []
    if not response:
        return None
    entry = response[0]
    status_short = ((entry.get("fixture") or {}).get("status") or {}).get("short")
    goals = entry.get("goals") or {}
    score = entry.get("score") or {}
    ht = score.get("halftime") or {}

    if status_short in FINISHED_STATUSES:
        result = {
            "status": "finished",
            "home_goals": goals.get("home"),
            "away_goals": goals.get("away"),
            "ht_home_goals": ht.get("home"),
            "ht_away_goals": ht.get("away"),
        }
        football_cache.set(cache_key, result)
        return result
    if status_short in VOID_STATUSES:
        result = {"status": "postponed" if status_short == "PST" else "cancelled"}
        football_cache.set(cache_key, result)
        return result
    return None  # still not started/in progress -- try again next cycle


def _prediction_and_event_result(row: Any, fetched: Dict[str, Any]) -> "tuple[Optional[Prediction], Optional[EventResult]]":
    mapping = MARKET_KEY_MAP.get(row["market"])
    if mapping is None:
        return None, None
    market_type, selection, line = mapping
    prediction = Prediction(
        sport=row["sport"] or "football", country=row["country"], league=row["league"],
        event_id=f"analytics:{row['fixture_id']}", event_start_time=row["match_datetime"] or "",
        home_team=row["home_team"] or "", away_team=row["away_team"] or "",
        market_type=market_type, market_name=row["market_label"] or row["market"], selection=selection,
        bookmaker_odds=row["odds"] or 1.0, model_probability=row["estimated_probability"] or 0.0,
        confidence_score=0.0, confidence_level=row["signal_level"] or "", recommendation_group="main",
        explanation=row["reason"] or "", data_provider="api_football", model_version=row["model_version"] or "",
        line=line,
    )
    result = EventResult(
        event_id=f"analytics:{row['fixture_id']}", status=fetched.get("status", "unknown"),
        home_goals=fetched.get("home_goals"), away_goals=fetched.get("away_goals"),
        ht_home_goals=fetched.get("ht_home_goals"), ht_away_goals=fetched.get("ht_away_goals"),
    )
    return prediction, result


def run_check_cycle(
    analytics_storage: AnalyticsStorage, football_cache: FootballCache, football_api_key: Optional[str],
    now: Optional[datetime.datetime] = None, *, stake: float = DEFAULT_STAKE,
) -> Dict[str, int]:
    """One pass: checks every pending prediction whose match is old enough,
    settles the ones whose result is now known, and rebuilds the
    materialized statistics tables. Returns a small summary dict."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = (now - datetime.timedelta(hours=RESULT_CHECK_MIN_HOURS_AFTER_KICKOFF)).isoformat()

    checked = settled = still_pending = errors = 0
    for row in analytics_storage.pending_predictions(before=cutoff):
        checked += 1
        fixture_id = row["fixture_id"]
        if fixture_id is None:
            errors += 1
            continue
        fetched = fetch_fixture_result(fixture_id, football_api_key, football_cache)
        if fetched is None:
            still_pending += 1
            continue

        prediction, event_result = _prediction_and_event_result(row, fetched)
        if prediction is None or event_result is None:
            logger.warning("analytics result checker: no market mapping for %r", row["market"])
            errors += 1
            continue

        status, explanation = settle_prediction(prediction, event_result)
        won, lost, void = _STATUS_MAP_TO_PROFIT_FLAGS.get(status, (False, False, False))
        profit = _profit_for(status, row["odds"], stake)
        wrote = analytics_storage.record_result(
            prediction_id=row["id"], fixture_id=fixture_id,
            final_home_score=event_result.home_goals, final_away_score=event_result.away_goals,
            status=status, won=won, lost=lost, void=void, profit=profit, stake=stake,
            settlement_explanation=explanation,
        )
        if wrote:
            settled += 1

    analytics_storage.refresh_statistics(stake=stake)
    return {"checked": checked, "settled": settled, "still_pending": still_pending, "errors": errors}
