"""
End-to-end orchestration for the "🤖 Прогнозы ИИ" feature:

  fetch real odds -> 36h window filter -> build real candidates (odds +
  statistics) -> selection_engine.select_recommendations -> persist MAIN
  picks to tracking (dedup-safe) -> render the Russian report text.

This is the only module allowed to import football/, selection_engine/,
and tracking/ together plus perform network I/O -- see
tests/test_ai_predictions_isolation.py for the exact boundary.
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass, field
from typing import List, Optional

from ai_predictions.candidate_builder import build_candidates_for_event
from ai_predictions.odds_client import fetch_football_events
from ai_predictions.window import filter_events_in_window
from football.providers.api_football import ApiFootballProvider
from selection_engine.config import (
    MARKET_1X2,
    MARKET_BTTS,
    MARKET_DOUBLE_CHANCE,
    MARKET_DRAW_NO_BET,
    MARKET_TOTAL_GOALS,
    SelectionConfig,
)
from selection_engine.models import CandidatePrediction
from selection_engine.report import render_daily_report
from selection_engine.selector import select_recommendations
from tracking.models import STATUS_PENDING, Prediction
from tracking.storage import DuplicatePredictionError, TrackingStorage

#: Hard cap on how many in-window events get real statistics pulled, to
#: keep one bot request from exhausting the API-Football daily quota.
MAX_EVENTS_ANALYZED = 12

#: Fallback data requirements used ONLY when the primary (statistics +
#: odds) pass produces zero MAIN/RESERVE recommendations. Drops the
#: statistics-derived fields (home_form/away_form/sample_size/...) from
#: "required" to "optional" so a candidate can still be evaluated purely
#: on real bookmaker consensus + real prices when football statistics are
#: genuinely unavailable for a run (e.g. the configured API-Football key
#: is on a plan tier that blocks the current season's fixtures). Optional
#: field lists are left untouched -- data_completeness still reflects the
#: real gap, it simply no longer causes a hard rejection. This never
#: invents a probability or a price; it only changes which real, already
#: -computed fields are mandatory versus merely nice-to-have.
ODDS_ONLY_REQUIREMENTS_OVERRIDE = {
    MARKET_1X2: {"required": [], "optional": ["home_form", "away_form", "sample_size", "h2h", "league_position", "injuries", "lineups"]},
    MARKET_DOUBLE_CHANCE: {"required": [], "optional": ["home_form", "away_form", "sample_size", "h2h", "league_position"]},
    MARKET_DRAW_NO_BET: {"required": [], "optional": ["home_form", "away_form", "sample_size", "h2h", "league_position"]},
    MARKET_BTTS: {"required": [], "optional": ["btts_frequency_home", "btts_frequency_away", "sample_size", "clean_sheets_home", "clean_sheets_away", "goals_scored_conceded"]},
    MARKET_TOTAL_GOALS: {"required": ["current_price"], "optional": ["goals_scored_conceded", "sample_size", "h2h", "league_position"]},
}

RECOMMENDATION_GROUP_TO_TRACKING = {
    "MAIN": "main",
    "RESERVE": "alternative",
    "HIGH_RISK": "high_risk",
    "AVOID": "avoid",
}

#: Russian display names for tracking.models.Prediction.market_name --
#: selection_engine works with technical market_type keys, tracking wants a
#: human-readable label.
MARKET_TYPE_DISPLAY_NAMES = {
    "1x2": "Исход матча (1X2)",
    "double_chance": "Двойной шанс",
    "draw_no_bet": "Без ничьей",
    "btts": "Обе забьют",
    "total_goals": "Тотал голов",
}

CONFIDENCE_LEVEL_BANDS = (
    (85.0, "очень высокая"),
    (75.0, "высокая"),
    (65.0, "средняя"),
    (55.0, "низкая"),
)


def _confidence_level(score: Optional[float]) -> str:
    if score is None:
        return "неизвестна"
    for threshold, label in CONFIDENCE_LEVEL_BANDS:
        if score >= threshold:
            return label
    return "минимальная"


@dataclass
class PipelineResult:
    report_text: str
    events_considered: int
    events_excluded_by_window: int
    candidates_considered: int
    saved_count: int
    duplicate_count: int
    errors: List[str] = field(default_factory=list)
    #: True when the primary (statistics + odds) pass found zero MAIN/
    #: RESERVE recommendations and the odds-only fallback (see
    #: ODDS_ONLY_REQUIREMENTS_OVERRIDE) was used instead. Always False
    #: when statistics were available -- this is a transparency flag, not
    #: a silent behavior change.
    used_odds_only_fallback: bool = False


def _event_id(event) -> str:
    return str(event.get("id") or f"{event.get('_sport_key')}|{event.get('commence_time')}|{event.get('home_team')}|{event.get('away_team')}")


def _save_main_predictions(
    main_candidates: List[CandidatePrediction], storage: TrackingStorage, *, odds_only: bool = False,
) -> "tuple[int, int]":
    saved, duplicates = 0, 0
    for candidate in main_candidates:
        prediction = Prediction(
            sport=candidate.sport,
            country=candidate.country,
            league=candidate.league,
            event_id=candidate.event_id,
            event_start_time=candidate.match_datetime,
            home_team=candidate.home_team,
            away_team=candidate.away_team,
            market_type=candidate.market_type,
            market_name=MARKET_TYPE_DISPLAY_NAMES.get(candidate.market_type, candidate.market_type),
            selection=candidate.selection,
            bookmaker_odds=candidate.odds,
            model_probability=candidate.model_probability,
            confidence_score=candidate.confidence_score or 0.0,
            confidence_level=_confidence_level(candidate.confidence_score),
            recommendation_group=RECOMMENDATION_GROUP_TO_TRACKING.get(candidate.recommendation_group or "MAIN", "main"),
            explanation="; ".join(candidate.explanation) or "н/д",
            data_provider="the_odds_api" if odds_only else "the_odds_api+api_football",
            model_version=candidate.model_version,
            line=candidate.line,
            status=STATUS_PENDING,
        )
        try:
            storage.save_prediction(prediction)
            saved += 1
        except DuplicatePredictionError:
            duplicates += 1
    return saved, duplicates


def run_ai_predictions(
    *,
    football_api_key: Optional[str] = None,
    odds_api_key: Optional[str] = None,
    config: Optional[SelectionConfig] = None,
    storage: Optional[TrackingStorage] = None,
    now: Optional[datetime.datetime] = None,
    max_events: int = MAX_EVENTS_ANALYZED,
) -> PipelineResult:
    """Runs the full live pipeline once. Never raises for expected failure
    modes (no odds, no stats, zero qualifying candidates) -- those show up
    as an honest "no recommendations today" report instead."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    football_api_key = football_api_key or os.getenv("FOOTBALL_API_KEY")
    config = config or SelectionConfig()
    owns_storage = storage is None
    storage = storage or TrackingStorage()

    events, _credits, odds_errors = fetch_football_events(api_key=odds_api_key)
    in_window, excluded = filter_events_in_window(events, now)
    in_window = in_window[:max_events]

    provider = ApiFootballProvider(api_key=football_api_key)

    raw_candidates: List[CandidatePrediction] = []
    for event in in_window:
        event_id = _event_id(event)
        league = event.get("sport_title") or event.get("_sport_key")
        try:
            candidates = build_candidates_for_event(event, provider, event_id=event_id, league=league)
        except Exception as exc:  # noqa: BLE001 -- one bad event must not sink the run
            odds_errors.append(f"Ошибка обработки события {event_id}: {exc}")
            continue
        raw_candidates.extend(candidates)

    result = select_recommendations(raw_candidates, config, storage=storage, now=now)
    used_fallback = False

    if not result.main and not result.reserve and raw_candidates:
        # Primary pass (real statistics + real odds) found nothing.
        # Retry once with real odds only -- never fabricates a price or a
        # probability, just stops requiring statistics fields that are
        # genuinely unavailable this run (e.g. API-Football plan/season
        # restrictions) to be treated as a hard rejection.
        fallback_config = SelectionConfig(
            **{**config.__dict__, "market_data_requirements_override": ODDS_ONLY_REQUIREMENTS_OVERRIDE}
        )
        fallback_candidates = [CandidatePrediction(**c.__dict__) for c in raw_candidates]
        fallback_result = select_recommendations(fallback_candidates, fallback_config, storage=storage, now=now)
        if fallback_result.main or fallback_result.reserve:
            result = fallback_result
            used_fallback = True
            for candidate in result.main + result.reserve:
                candidate.explanation = list(candidate.explanation) + [
                    "Реальная статистика недоступна для этого запуска (ограничение плана API-Football) "
                    "— оценка основана только на реальных коэффициентах букмекеров."
                ]

    report_text = render_daily_report(result)
    if used_fallback:
        report_text = (
            "⚠️ Статистика от API-Football недоступна для этого запуска (ограничение тарифного плана). "
            "Ниже — рекомендации на основе реальных коэффициентов букмекеров без статистического блендинга.\n\n"
            + report_text
        )

    saved, duplicates = _save_main_predictions(result.main, storage, odds_only=used_fallback)

    if owns_storage:
        storage.close()

    return PipelineResult(
        report_text=report_text,
        events_considered=len(in_window),
        events_excluded_by_window=excluded,
        candidates_considered=len(raw_candidates),
        saved_count=saved,
        duplicate_count=duplicates,
        errors=odds_errors,
        used_odds_only_fallback=used_fallback,
    )
