"""
Live in-play predictions mode (2026-07-15, Task #11) -- orchestration.

Completely independent of ai_predictions/football_pipeline.py: never
reads or writes the shared daily archive/pool key, never shares its
cache key, and can run/fail/succeed without affecting the "🤖 Прогнозы
ИИ" button in any way (see tests/test_live_pipeline.py's isolation
assertions).

Pipeline:
  discover real in-progress fixtures (API-Football, live_fixtures.py)
  -> fetch every currently active real Odds API event once
     (ai_predictions/odds_client.py -- the same endpoint The Odds API
     serves in-play odds through once a match has started)
  -> match live fixtures to real events by team-name + kickoff-time
     confidence (ai_predictions/fixture_matching.py, duck-typed on
     LiveFixture)
  -> build one real cross-bookmaker-consensus candidate per matched
     fixture (ai_predictions/live_candidates.py, reusing
     ai_predictions/value_engine.py's math) -- drop fixtures with no
     matched real price or no candidate clearing even LOW
  -> rank by ranking_score, cap at LIVE_MAX_RECOMMENDATIONS
  -> persist to tracking + analytics with mode="live" (only on a real
     fetch, never on a cache replay)
  -> render the Live-specific Russian card format
  -> cache the full rendered result for LIVE_CACHE_TTL_MINUTES so a
     press within that window costs no further API-Football/Odds API
     quota.
"""

from __future__ import annotations

import datetime
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ai_predictions.fixture_matching import match_fixtures_to_events
from ai_predictions.football_cache import FootballCache
from ai_predictions.live_candidates import LiveCandidate, build_live_candidates
from ai_predictions.live_fixtures import LiveFixtureDiscoveryResult, discover_live_fixtures
from ai_predictions.live_report import render_live_message
from ai_predictions.odds_client import fetch_all_active_football_events
from ai_predictions.value_config import (
    BET_MARKET_LABELS_RU,
    LIVE_MAX_RECOMMENDATIONS,
    PREDICTION_MODE_LIVE,
)
from tracking.models import STATUS_PENDING, Prediction
from tracking.storage import DuplicatePredictionError, TrackingStorage

from analytics.integration import record_recommendation
from analytics.storage import AnalyticsStorage

logger = logging.getLogger(__name__)

LIVE_PIPELINE_MODEL_VERSION = "live-predictions-v1"

#: Own cache key, deliberately disjoint from football_pipeline.py's
#: DAILY_ARCHIVE_KEY -- Live mode never reads/writes the shared pool.
LIVE_CACHE_KEY = "live_predictions:cache_v1"


@dataclass
class LivePipelineResult:
    telegram_messages: List[str] = field(default_factory=list)
    live_fixture_count: int = 0
    matched_fixture_count: int = 0
    recommendations_count: int = 0
    saved_count: int = 0
    duplicate_count: int = 0
    errors: List[str] = field(default_factory=list)
    generated_at: Optional[datetime.datetime] = None
    from_cache: bool = False


def _prediction_from_live_candidate(lc: LiveCandidate) -> Prediction:
    c = lc.value_candidate
    lf = lc.live_fixture
    return Prediction(
        sport="football",
        country=lf.league_country,
        league=lf.league_name,
        event_id=f"api_football:{lf.fixture_id}",
        event_start_time=lf.kickoff_utc.isoformat(),
        home_team=lf.home_team,
        away_team=lf.away_team,
        market_type=c.market_type,
        market_name=BET_MARKET_LABELS_RU.get(c.market_type, c.market_type),
        selection=c.selection,
        bookmaker_odds=c.best_price,
        model_probability=c.consensus_probability,
        confidence_score=round(c.consensus_probability * 100.0, 1),
        confidence_level=c.signal_level.lower(),
        # RECOMMENDATION_GROUPS (tracking/models.py) is a pre-match-era
        # enum with no "live" member; Live mode is distinguished from
        # pre-match by `mode=PREDICTION_MODE_LIVE` below, not by this
        # field, so every live pick maps into "main" (never invents a
        # new enum member here since it is not the mode marker itself).
        recommendation_group="main",
        explanation=(
            f"Live-сигнал по ходу матча (минута {lf.elapsed_minutes}, счёт "
            f"{lf.home_score}:{lf.away_score}): консенсус нескольких букмекеров, "
            f"уровень сигнала {c.signal_level}."
        ),
        data_provider="api_football+the_odds_api",
        model_version=LIVE_PIPELINE_MODEL_VERSION,
        status=STATUS_PENDING,
        signal_level=c.signal_level,
        ranking_score=c.ranking_score,
        fixture_id=lf.fixture_id,
        market_probability=c.consensus_probability,
        mode=PREDICTION_MODE_LIVE,
    )


def _persist_live_candidates(
    live_candidates: List[LiveCandidate],
    storage: TrackingStorage,
    analytics_storage: AnalyticsStorage,
    now: datetime.datetime,
) -> "tuple[int, int]":
    saved, duplicates = 0, 0
    for lc in live_candidates:
        prediction = _prediction_from_live_candidate(lc)
        try:
            storage.save_prediction(prediction)
            saved += 1
        except DuplicatePredictionError:
            duplicates += 1
        record_recommendation(
            analytics_storage, _RecommendationAdapter(lc), lc.value_candidate.best_price,
            model_version=LIVE_PIPELINE_MODEL_VERSION,
            archive_version=now.date().isoformat(), now=now, mode=PREDICTION_MODE_LIVE,
        )
    return saved, duplicates


class _RecommendationAdapter:
    """analytics.integration.record_recommendation expects a
    RankedRecommendation-shaped object (`.candidate` with
    `.fixture/.market_key/.probability/.rationale/...` and
    `.signal_level`) -- Live mode's real candidate shape
    (LiveCandidate/ValueCandidate) is structurally different (it comes
    from value_engine.py, not football_predictions.py), so this thin
    adapter maps the real fields across without inventing anything."""

    def __init__(self, lc: LiveCandidate) -> None:
        self.candidate = _CandidateAdapter(lc)
        self.signal_level = lc.value_candidate.signal_level


class _CandidateAdapter:
    def __init__(self, lc: LiveCandidate) -> None:
        self.fixture = _FixtureAdapter(lc.live_fixture)
        c = lc.value_candidate
        self.market_key = c.market_type
        self.market_label_ru = BET_MARKET_LABELS_RU.get(c.market_type, c.market_type)
        self.probability = c.consensus_probability
        self.rationale = (
            f"Live-сигнал: минута {lc.live_fixture.elapsed_minutes}, счёт "
            f"{lc.live_fixture.home_score}:{lc.live_fixture.away_score}, "
            f"уровень сигнала {c.signal_level}."
        )


class _FixtureAdapter:
    def __init__(self, live_fixture) -> None:
        self.kickoff_utc = live_fixture.kickoff_utc
        self.league_country = live_fixture.league_country
        self.league_name = live_fixture.league_name
        self.fixture_id = live_fixture.fixture_id
        self.home_team = live_fixture.home_team
        self.away_team = live_fixture.away_team


def _serialize_result(result: LivePipelineResult) -> Dict[str, Any]:
    return {
        "telegram_messages": result.telegram_messages,
        "live_fixture_count": result.live_fixture_count,
        "matched_fixture_count": result.matched_fixture_count,
        "recommendations_count": result.recommendations_count,
        "saved_count": result.saved_count,
        "duplicate_count": result.duplicate_count,
        "errors": result.errors,
        "generated_at": result.generated_at.isoformat() if result.generated_at else None,
    }


def _deserialize_result(payload: Dict[str, Any]) -> LivePipelineResult:
    generated_at = None
    if payload.get("generated_at"):
        try:
            generated_at = datetime.datetime.fromisoformat(payload["generated_at"])
        except ValueError:
            generated_at = None
    return LivePipelineResult(
        telegram_messages=payload.get("telegram_messages", []),
        live_fixture_count=payload.get("live_fixture_count", 0),
        matched_fixture_count=payload.get("matched_fixture_count", 0),
        recommendations_count=payload.get("recommendations_count", 0),
        saved_count=payload.get("saved_count", 0),
        duplicate_count=payload.get("duplicate_count", 0),
        errors=payload.get("errors", []),
        generated_at=generated_at,
        from_cache=True,
    )


def load_cached_live_result(football_cache: FootballCache, ttl_minutes: float) -> Optional[LivePipelineResult]:
    payload = football_cache.get(LIVE_CACHE_KEY, ttl_hours=ttl_minutes / 60.0)
    if payload is None:
        return None
    return _deserialize_result(payload)


def cache_age_seconds(football_cache: FootballCache, now: datetime.datetime) -> Optional[float]:
    """For /status -- age of the last Live fetch regardless of TTL
    (mirrors football_cache.cached_at's "ignore TTL" contract)."""
    cached_at = football_cache.cached_at(LIVE_CACHE_KEY)
    if cached_at is None:
        return None
    return (now - cached_at).total_seconds()


_UNSET = object()


def run_live_predictions(
    *,
    football_api_key: Any = _UNSET,
    odds_api_key: Optional[str] = None,
    storage: Optional[TrackingStorage] = None,
    now: Optional[datetime.datetime] = None,
    football_cache: Optional[FootballCache] = None,
    analytics_storage: Optional[AnalyticsStorage] = None,
    live_fixture_provider_factory: Any = None,
) -> LivePipelineResult:
    """Always spends a real fetch (no cache check here) -- the cache
    layer lives one level up in bot.py's handler, exactly mirroring how
    football_pipeline.py separates "run a real analysis" from "decide
    whether a cached one is still fresh enough to reuse"."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if football_api_key is _UNSET:
        football_api_key = os.getenv("FOOTBALL_API_KEY")
    odds_api_key = odds_api_key if odds_api_key is not None else os.getenv("ODDS_API_KEY")

    owns_storage = storage is None
    storage = storage or TrackingStorage()
    owns_football_cache = football_cache is None
    football_cache = football_cache or FootballCache(now=now)
    owns_analytics_storage = analytics_storage is None
    analytics_storage = analytics_storage or AnalyticsStorage(now=now)

    result = LivePipelineResult(generated_at=now)

    try:
        discovery: LiveFixtureDiscoveryResult = discover_live_fixtures(
            football_api_key, football_cache, provider_factory=live_fixture_provider_factory,
        )
        result.errors.extend(discovery.errors)
        result.live_fixture_count = len(discovery.fixtures)

        if not discovery.fixtures:
            result.telegram_messages = render_live_message([], live_fixture_count=0, matched_fixture_count=0)
            return result

        if not odds_api_key:
            result.errors.append("Не найден ODDS_API_KEY")
            result.telegram_messages = render_live_message(
                [], live_fixture_count=result.live_fixture_count, matched_fixture_count=0,
            )
            return result

        odds_fetch = fetch_all_active_football_events(api_key=odds_api_key, persistent_cache=football_cache)
        result.errors.extend(odds_fetch.errors)

        match_result = match_fixtures_to_events(discovery.fixtures, odds_fetch.events)
        result.matched_fixture_count = len(match_result.matches)

        if not match_result.matches:
            result.telegram_messages = render_live_message(
                [], live_fixture_count=result.live_fixture_count, matched_fixture_count=0,
            )
            return result

        live_candidates = build_live_candidates(match_result.matches)
        live_candidates.sort(key=lambda lc: lc.value_candidate.ranking_score, reverse=True)
        live_candidates = live_candidates[:LIVE_MAX_RECOMMENDATIONS]
        result.recommendations_count = len(live_candidates)

        result.telegram_messages = render_live_message(
            live_candidates,
            live_fixture_count=result.live_fixture_count,
            matched_fixture_count=result.matched_fixture_count,
        )

        saved, duplicates = _persist_live_candidates(live_candidates, storage, analytics_storage, now)
        result.saved_count = saved
        result.duplicate_count = duplicates
        return result
    finally:
        if owns_storage:
            storage.close()
        if owns_analytics_storage:
            analytics_storage.close()
        if owns_football_cache:
            football_cache.close()


def run_live_predictions_cached(
    *,
    football_api_key: Any = _UNSET,
    odds_api_key: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
    football_cache: Optional[FootballCache] = None,
    ttl_minutes: float = 10.0,
    **kwargs: Any,
) -> LivePipelineResult:
    """Entry point bot.py's handler calls: reuses the persisted Live
    result if it is still within `ttl_minutes`, otherwise runs a real
    fetch and caches it. `football_cache` is never closed here when
    passed in by the caller (bot.py owns it for the lifetime of the
    request in that case)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    owns_football_cache = football_cache is None
    football_cache = football_cache or FootballCache(now=now)
    try:
        cached = load_cached_live_result(football_cache, ttl_minutes)
        if cached is not None:
            return cached

        result = run_live_predictions(
            football_api_key=football_api_key, odds_api_key=odds_api_key, now=now,
            football_cache=football_cache, **kwargs,
        )
        football_cache.set(LIVE_CACHE_KEY, _serialize_result(result))
        return result
    finally:
        if owns_football_cache:
            football_cache.close()
