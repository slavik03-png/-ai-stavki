"""
Production v3 orchestration: API-Football is the PRIMARY and SUFFICIENT
data source for the "🤖 Прогнозы ИИ" recommendations. The Odds API is
purely optional coefficient enrichment layered on afterwards -- see
module docstrings on ai_predictions/football_predictions.py and
ai_predictions/odds_lookup.py for the exact non-blocking contract.

Pipeline:
  discover real fixtures in the strict 36h window (API-Football only,
  6h-cached) -> analyse up to MAX_FIXTURES_ANALYSED_PER_RUN of them
  (soonest kickoff first) purely from API-Football data -> pick the
  single best real market per fixture, classify HIGH/MEDIUM/LOW/omit
  by the probability+completeness thresholds, keep up to 5 -> best-
  effort attach a real Odds API coefficient to each kept recommendation
  (never blocking) -> persist every kept recommendation to tracking ->
  render the exact Russian card format.
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ai_predictions.fixtures import FixtureDiscoveryResult, discover_fixtures_in_window
from ai_predictions.football_cache import FootballCache
from ai_predictions.football_predictions import MarketCandidate, build_candidates_for_fixture
from ai_predictions.odds_lookup import OddsLookupResult, lookup_coefficients
from ai_predictions.prediction_report import render_predictions_message
from ai_predictions.prediction_selector import RankedRecommendation, select_recommendations
from ai_predictions.value_config import (
    BET_MARKET_LABELS_RU,
    DAILY_ARCHIVE_LOCK_TTL_MINUTES,
    DAILY_ARCHIVE_TTL_HOURS,
    MAX_FIXTURES_ANALYSED_PER_RUN,
    SIGNAL_HIGH,
    SIGNAL_LOW,
    SIGNAL_MEDIUM,
)
from tracking.models import STATUS_PENDING, Prediction
from tracking.storage import DuplicatePredictionError, TrackingStorage

FOOTBALL_PIPELINE_MODEL_VERSION = "football-predictions-v3"

_LEVEL_TO_RECOMMENDATION_GROUP = {SIGNAL_HIGH: "main", SIGNAL_MEDIUM: "alternative", SIGNAL_LOW: "high_risk"}

_UNSET = object()

#: Persistent key for the strict daily archive (see module docstring in
#: value_config.py, "Strict daily archive"). Fixed window (36h) is baked
#: into the key name itself since the window is a constant, not a
#: parameter, in this production version -- if that ever changes, the key
#: must change with it so an old-window archive is never replayed as if
#: it were computed for a new window.
DAILY_ARCHIVE_KEY = "daily_archive:predictions_top5_window36h"

#: Marker key used to detect a refresh already under way (see
#: mark_refresh_in_progress/is_refresh_in_progress below) -- separate
#: from the archive key itself so a crashed/slow run never looks like a
#: successful archive.
DAILY_ARCHIVE_LOCK_KEY = "daily_archive:refresh_in_progress"


@dataclass
class FootballPipelineResult:
    telegram_messages: List[str] = field(default_factory=list)
    found_fixtures: int = 0
    analysed_fixtures: int = 0
    fully_stat_fixtures: int = 0
    recommendations_count: int = 0
    api_football_requests_used: int = 0
    api_football_requests_remaining: int = 0
    api_football_requests_used_today: int = 0
    odds_status: str = "unavailable"  # available | quota_exhausted | unavailable
    fixture_discovery: Optional[FixtureDiscoveryResult] = None
    recommendations: List[RankedRecommendation] = field(default_factory=list)
    odds_by_fixture: Dict[int, float] = field(default_factory=dict)
    saved_count: int = 0
    duplicate_count: int = 0
    errors: List[str] = field(default_factory=list)


@dataclass
class DailyArchive:
    """A previously computed daily result, replayed verbatim on later
    button presses within the same 24h window -- zero recomputation, zero
    API-Football calls."""
    messages: List[str]
    diagnostics: Dict[str, Any]
    generated_at: datetime.datetime


def load_daily_archive(
    football_cache: FootballCache, now: datetime.datetime, *, ignore_ttl: bool = False,
) -> Optional[DailyArchive]:
    """Returns the persisted daily result if it is still within
    DAILY_ARCHIVE_TTL_HOURS, else None. `ignore_ttl=True` is used only for
    the "a refresh is already in progress -- fall back to whatever we
    have" path, so a user still gets something useful instead of a bare
    "please wait"."""
    ttl = 24.0 * 365 if ignore_ttl else DAILY_ARCHIVE_TTL_HOURS
    payload = football_cache.get(DAILY_ARCHIVE_KEY, ttl_hours=ttl)
    if payload is None:
        return None
    try:
        generated_at = datetime.datetime.fromisoformat(payload["generated_at"])
    except (KeyError, ValueError):
        return None
    return DailyArchive(messages=payload["messages"], diagnostics=payload.get("diagnostics", {}), generated_at=generated_at)


def save_daily_archive(football_cache: FootballCache, result: "FootballPipelineResult", now: datetime.datetime) -> None:
    diagnostics = {
        "found_fixtures": result.found_fixtures,
        "analysed_fixtures": result.analysed_fixtures,
        "fully_stat_fixtures": result.fully_stat_fixtures,
        "recommendations_count": result.recommendations_count,
        "api_football_requests_used": result.api_football_requests_used,
        "api_football_requests_remaining": result.api_football_requests_remaining,
        "api_football_requests_used_today": result.api_football_requests_used_today,
        "odds_status": result.odds_status,
        "errors": result.errors,
        "source": "новый запрос",
    }
    football_cache.set(DAILY_ARCHIVE_KEY, {
        "messages": result.telegram_messages,
        "diagnostics": diagnostics,
        "generated_at": now.isoformat(),
    })


def mark_refresh_in_progress(football_cache: FootballCache, now: datetime.datetime) -> None:
    football_cache.set(DAILY_ARCHIVE_LOCK_KEY, {"started_at": now.isoformat()})


def is_refresh_in_progress(football_cache: FootballCache, now: datetime.datetime) -> bool:
    """True if some process (this one or another) started a refresh in
    the last DAILY_ARCHIVE_LOCK_TTL_MINUTES and has not finished it yet
    (a finished run always overwrites the daily archive itself, which
    callers should check FIRST -- this is purely the "someone else is
    mid-run right now" signal for requirement 11: never start a second
    concurrent batch of API-Football requests)."""
    return football_cache.get(DAILY_ARCHIVE_LOCK_KEY, ttl_hours=DAILY_ARCHIVE_LOCK_TTL_MINUTES / 60.0) is not None


def _recommendation_to_prediction(rec: RankedRecommendation, odds: Optional[float]) -> Prediction:
    c = rec.candidate
    fixture = c.fixture
    has_real_odds = odds is not None
    bookmaker_odds = odds if has_real_odds else round(1.0 / c.probability, 4) if c.probability > 0 else 1.0
    explanation = c.rationale
    return Prediction(
        sport="football",
        country=fixture.league_country,
        league=fixture.league_name,
        event_id=f"api_football:{fixture.fixture_id}",
        event_start_time=fixture.kickoff_utc.isoformat(),
        home_team=fixture.home_team,
        away_team=fixture.away_team,
        market_type=c.market_key,
        market_name=BET_MARKET_LABELS_RU.get(c.market_key, c.market_key),
        selection=c.market_key,
        bookmaker_odds=bookmaker_odds,
        model_probability=c.probability,
        confidence_score=round(c.probability * 100.0, 1),
        confidence_level=c.sample_size_category,
        recommendation_group=_LEVEL_TO_RECOMMENDATION_GROUP.get(rec.signal_level, "high_risk"),
        explanation=explanation,
        data_provider="api_football" if not has_real_odds else "api_football+the_odds_api",
        model_version=FOOTBALL_PIPELINE_MODEL_VERSION,
        status=STATUS_PENDING,
        signal_level=rec.signal_level,
        ranking_score=c.probability,
        statistics_completeness=c.completeness,
        sample_size_category=c.sample_size_category,
        fixture_id=fixture.fixture_id,
        market_probability=c.probability,
    )


def run_football_predictions(
    *,
    football_api_key: Any = _UNSET,
    odds_api_key: Optional[str] = None,
    storage: Optional[TrackingStorage] = None,
    now: Optional[datetime.datetime] = None,
    max_fixtures_analysed: int = MAX_FIXTURES_ANALYSED_PER_RUN,
    football_cache: Optional[FootballCache] = None,
) -> FootballPipelineResult:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if football_api_key is _UNSET:
        football_api_key = os.getenv("FOOTBALL_API_KEY")
    odds_api_key = odds_api_key if odds_api_key is not None else os.getenv("ODDS_API_KEY")

    owns_storage = storage is None
    storage = storage or TrackingStorage()
    owns_football_cache = football_cache is None
    football_cache = football_cache or FootballCache(now=now)

    result = FootballPipelineResult()

    fixture_discovery = discover_fixtures_in_window(football_api_key, football_cache, now)
    result.fixture_discovery = fixture_discovery
    result.found_fixtures = len(fixture_discovery.fixtures)
    result.errors.extend(fixture_discovery.errors)

    from football.providers.api_football import ApiFootballProvider
    provider = ApiFootballProvider(api_key=football_api_key, now=now)

    # Every fixture up to max_fixtures_analysed is always analysed --
    # build_candidates_for_fixture never needs to be skipped wholesale:
    # it reads persistent cache first, only spends real requests while
    # football_cache.can_spend(1) allows it (per real HTTP call, not per
    # fixture), and falls back to a historical-baseline signal when
    # nothing real is available at all. This guarantees analysed_fixtures
    # is never 0 while found_fixtures > 0 -- the daily quota reserve can
    # only reduce *how much real data* backs each candidate, never how
    # many fixtures get ranked.
    all_candidates: List[MarketCandidate] = []
    analysed = 0
    fully_stat_fixture_ids: set = set()
    quota_exhausted_during_run = False
    for fixture in fixture_discovery.fixtures[:max_fixtures_analysed]:
        if not football_cache.can_spend(1):
            quota_exhausted_during_run = True
        candidates, _ = build_candidates_for_fixture(fixture, provider, football_cache)
        all_candidates.extend(candidates)
        analysed += 1
        if any(c.source != "historical_baseline" for c in candidates):
            fully_stat_fixture_ids.add(fixture.fixture_id)

    if quota_exhausted_during_run:
        result.errors.append(
            f"Резерв запросов к API-Football на сегодня исчерпан во время анализа — "
            f"часть из {analysed} проанализированных матчей использует статистику из кэша "
            f"или обобщённые исторические данные вместо свежих запросов."
        )

    result.analysed_fixtures = analysed
    result.fully_stat_fixtures = len(fully_stat_fixture_ids)
    result.api_football_requests_used = provider.requests_made
    result.api_football_requests_remaining = football_cache.requests_available()
    result.api_football_requests_used_today = football_cache.requests_used_today()

    ranked = select_recommendations(all_candidates)
    result.recommendations = ranked
    result.recommendations_count = len(ranked)

    fixture_market_keys = {rec.candidate.fixture.fixture_id: rec.candidate.market_key for rec in ranked}
    fixtures_for_lookup = [rec.candidate.fixture for rec in ranked]
    try:
        odds_result = lookup_coefficients(fixtures_for_lookup, fixture_market_keys, odds_api_key=odds_api_key)
    except Exception as exc:  # never let optional enrichment break the run
        odds_result = OddsLookupResult(prices_by_fixture={}, status="unavailable", detail=str(exc))
    result.odds_status = odds_result.status
    result.odds_by_fixture = odds_result.prices_by_fixture

    result.telegram_messages = render_predictions_message(
        ranked, result.odds_by_fixture,
        found_fixtures=result.found_fixtures, analysed_fixtures=result.analysed_fixtures,
    )

    saved, duplicates = 0, 0
    for rec in ranked:
        odds = result.odds_by_fixture.get(rec.candidate.fixture.fixture_id)
        prediction = _recommendation_to_prediction(rec, odds)
        try:
            storage.save_prediction(prediction)
            saved += 1
        except DuplicatePredictionError:
            duplicates += 1
    result.saved_count = saved
    result.duplicate_count = duplicates

    if owns_storage:
        storage.close()
    if owns_football_cache:
        football_cache.close()

    return result
