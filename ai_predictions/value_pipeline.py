"""
End-to-end orchestration for the cross-bookmaker value-detection strategy:

  fetch real odds -> 36h window filter -> build real ValueCandidates from
  cross-bookmaker price divergence -> select up to 5, one per event ->
  persist to tracking (dedup-safe) -> render the Russian report text.

Deliberately does not call football/ or ApiFootballProvider at all: this
strategy needs no team statistics. The API-Football integration
(football/providers/api_football.py) is left completely untouched and
available for ai_predictions/pipeline.py (the statistics-based strategy)
once a paid plan makes current-season data available.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import List, Optional

from ai_predictions.odds_client import fetch_football_events
from ai_predictions.value_engine import ValueCandidate, build_value_candidates_for_event
from ai_predictions.value_report import Diagnostics, render_value_report
from ai_predictions.value_selector import select_value_recommendations
from ai_predictions.window import filter_events_in_window
from tracking.models import STATUS_PENDING, Prediction
from tracking.storage import DuplicatePredictionError, TrackingStorage

MARKET_TYPE_DISPLAY_NAMES = {
    "1x2": "Исход матча (1X2)",
    "double_chance": "Двойной шанс",
    "draw_no_bet": "Без ничьей",
    "total_goals": "Тотал голов",
}

#: A confidence_level bucket derived purely from real, observable market
#: agreement (bookmaker_count) -- explicitly NOT a statistical confidence
#: claim about the match outcome, only about how many independent real
#: bookmakers back the divergence.
def _confidence_level(bookmaker_count: int) -> str:
    if bookmaker_count >= 6:
        return "высокое согласие рынка"
    if bookmaker_count >= 4:
        return "среднее согласие рынка"
    return "минимальное согласие рынка"


@dataclass
class ValuePipelineResult:
    report_text: str
    events_received: int
    events_excluded_by_window: int
    markets_compared: int
    candidates_created: int
    candidates_rejected: int
    final_recommendations: int
    saved_count: int
    duplicate_count: int
    errors: List[str] = field(default_factory=list)


def _event_id(event) -> str:
    return str(event.get("id") or f"{event.get('_sport_key')}|{event.get('commence_time')}|{event.get('home_team')}|{event.get('away_team')}")


def _save_recommendations(main: List[ValueCandidate], storage: TrackingStorage) -> "tuple[int, int]":
    saved, duplicates = 0, 0
    for candidate in main:
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
            bookmaker_odds=candidate.best_price,
            model_probability=candidate.consensus_probability,
            confidence_score=round(max(0.0, min(100.0, candidate.edge * 100.0)), 1),
            confidence_level=_confidence_level(candidate.bookmaker_count),
            recommendation_group="main",
            explanation=(
                f"Лучшая цена {candidate.best_price:.2f} у {candidate.best_bookmaker} против справедливой "
                f"{candidate.fair_price:.2f} по {candidate.consensus_bookmaker_count} другим букмекерам "
                f"(расхождение {candidate.edge:.3f}, всего {candidate.bookmaker_count} букмекеров по исходу)."
            ),
            data_provider="the_odds_api",
            model_version="value-divergence-v1.0",
            line=candidate.line,
            status=STATUS_PENDING,
        )
        try:
            storage.save_prediction(prediction)
            saved += 1
        except DuplicatePredictionError:
            duplicates += 1
    return saved, duplicates


def run_value_predictions(
    *,
    odds_api_key: Optional[str] = None,
    storage: Optional[TrackingStorage] = None,
    now: Optional[datetime.datetime] = None,
    max_events: Optional[int] = None,
) -> ValuePipelineResult:
    """Runs the full cross-bookmaker value-detection pipeline once. Never
    raises for expected failure modes (no odds, no qualifying divergence)
    -- those show up as an honest "no recommendations today" report."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    owns_storage = storage is None
    storage = storage or TrackingStorage()

    events, _credits, odds_errors = fetch_football_events(api_key=odds_api_key)
    in_window, excluded = filter_events_in_window(events, now)
    if max_events is not None:
        in_window = in_window[:max_events]

    all_candidates: List[ValueCandidate] = []
    markets_compared = 0
    for event in in_window:
        event_id = _event_id(event)
        league = event.get("sport_title") or event.get("_sport_key")
        try:
            candidates = build_value_candidates_for_event(event, event_id=event_id, league=league)
        except Exception as exc:  # noqa: BLE001 -- one bad event must not sink the run
            odds_errors.append(f"Ошибка обработки события {event_id}: {exc}")
            continue
        markets_compared += len({c.market_type for c in candidates})
        all_candidates.extend(candidates)

    result = select_value_recommendations(all_candidates)

    diagnostics = Diagnostics()
    diagnostics.events_received = len(events)
    diagnostics.markets_compared = markets_compared
    diagnostics.candidates_created = len(all_candidates)
    diagnostics.candidates_rejected = len(result.rejected)
    diagnostics.final_recommendations = len(result.main)

    report_text = render_value_report(result, diagnostics)

    saved, duplicates = _save_recommendations(result.main, storage)

    if owns_storage:
        storage.close()

    return ValuePipelineResult(
        report_text=report_text,
        events_received=len(events),
        events_excluded_by_window=excluded,
        markets_compared=markets_compared,
        candidates_created=len(all_candidates),
        candidates_rejected=len(result.rejected),
        final_recommendations=len(result.main),
        saved_count=saved,
        duplicate_count=duplicates,
        errors=odds_errors,
    )
