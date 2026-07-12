"""
End-to-end orchestration for the cross-bookmaker value-detection strategy:

  fetch real odds -> 36h window filter -> extract/validate/dedupe/group
  real bookmaker rows (ai_predictions/matching.py) -> build real
  ValueCandidates from cross-bookmaker price divergence
  (ai_predictions/value_engine.py) -> select up to 5, one per event ->
  persist to tracking (dedup-safe) -> render the Russian report text with
  full pipeline diagnostics.

Deliberately does not call football/ or ApiFootballProvider at all: this
strategy needs no team statistics. The API-Football integration
(football/providers/api_football.py) is left completely untouched and
available for ai_predictions/pipeline.py (the statistics-based strategy)
once a paid plan makes current-season data available.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ai_predictions.matching import (
    MarketGroup,
    ValidationStats,
    dedupe_bookmaker_rows,
    extract_rows,
    group_rows,
    validate_rows,
)
from ai_predictions.odds_client import fetch_football_events
from ai_predictions.value_engine import (
    MIN_BOOKMAKERS,
    ValueCandidate,
    build_value_candidates_from_groups,
)
from ai_predictions.value_report import Diagnostics, compute_top_rejection_reasons, render_value_report
from ai_predictions.value_selector import select_value_recommendations
from ai_predictions.window import filter_events_in_window
from tracking.models import STATUS_PENDING, Prediction
from tracking.storage import DuplicatePredictionError, TrackingStorage

MARKET_TYPE_DISPLAY_NAMES = {
    "1x2": "Исход матча (1X2)",
    "double_chance": "Двойной шанс",
    "draw_no_bet": "Без ничьей",
    "total_goals": "Тотал голов",
    "spread": "Гандикап",
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
    diagnostics: Optional[Diagnostics] = None
    debug_groups: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def _event_id(event: Dict[str, Any]) -> str:
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
            model_version="value-divergence-v1.1",
            line=candidate.line,
            status=STATUS_PENDING,
        )
        try:
            storage.save_prediction(prediction)
            saved += 1
        except DuplicatePredictionError:
            duplicates += 1
    return saved, duplicates


def _debug_group_lines(groups: Dict[Any, MarketGroup], limit: int = 10) -> List[str]:
    """Renders up to `limit` real groups with full detail (event, market,
    point, outcome, bookmakers, unique bookmaker count) -- used when
    markets_matched == 0 so a genuinely empty/off-season run and a broken
    matching pipeline are never visually indistinguishable."""
    lines = []
    for group in list(groups.values())[:limit]:
        for outcome, entries in group.outcomes.items():
            bookmakers = sorted({bm for bm, _, _ in entries})
            lines.append(
                f"{group.home_team} vs {group.away_team} | {group.market} "
                f"point={group.point} outcome={outcome} | букмекеров={len(bookmakers)} "
                f"({', '.join(bookmakers)})"
            )
    return lines[:limit]


def run_value_predictions(
    *,
    odds_api_key: Optional[str] = None,
    storage: Optional[TrackingStorage] = None,
    now: Optional[datetime.datetime] = None,
    max_events: Optional[int] = None,
) -> ValuePipelineResult:
    """Runs the full cross-bookmaker value-detection pipeline once. Never
    raises for expected failure modes (no odds, no qualifying divergence)
    -- those show up as an honest "no recommendations today" report, with
    full diagnostics distinguishing "off-season / no events in window"
    from "events present but nothing matched"."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    owns_storage = storage is None
    storage = storage or TrackingStorage()

    events, _credits, odds_errors = fetch_football_events(api_key=odds_api_key)
    in_window, excluded = filter_events_in_window(events, now)
    if max_events is not None:
        in_window = in_window[:max_events]

    stats = ValidationStats()
    all_rows = []
    for event in in_window:
        event_id = _event_id(event)
        league = event.get("sport_title") or event.get("_sport_key")
        try:
            all_rows.extend(extract_rows(event, event_id=event_id, league=league))
        except Exception as exc:  # noqa: BLE001 -- one bad event must not sink the run
            odds_errors.append(f"Ошибка обработки события {event_id}: {exc}")

    valid_rows = validate_rows(all_rows, stats)
    deduped_rows = dedupe_bookmaker_rows(valid_rows, stats)
    groups = group_rows(deduped_rows)

    unique_events = len({row.event_key for row in deduped_rows})
    groups_1, groups_2, groups_3plus, markets_matched = 0, 0, 0, 0
    for group in groups.values():
        max_bm = max((len({bm for bm, _, _ in entries}) for entries in group.outcomes.values()), default=0)
        if max_bm <= 1:
            groups_1 += 1
        elif max_bm == 2:
            groups_2 += 1
        else:
            groups_3plus += 1
        if max_bm >= MIN_BOOKMAKERS:
            markets_matched += 1

    all_candidates = build_value_candidates_from_groups(groups)
    result = select_value_recommendations(all_candidates)

    diagnostics = Diagnostics(
        events_received=len(events),
        events_excluded_by_window=excluded,
        events_in_window=len(in_window),
        rows_total=stats.rows_total,
        rows_valid=stats.rows_valid,
        unique_events=unique_events,
        unique_groups=len(groups),
        groups_with_1_bookmaker=groups_1,
        groups_with_2_bookmakers=groups_2,
        groups_with_3plus_bookmakers=groups_3plus,
        markets_matched=markets_matched,
        candidates_created=len(all_candidates),
        candidates_rejected=len(result.rejected),
        final_recommendations=len(result.main),
        duplicate_bookmaker_rows=stats.duplicate_bookmaker_rows,
        unsupported_markets_seen=dict(stats.unsupported_markets_seen),
        top_rejection_reasons=compute_top_rejection_reasons(result.rejected),
    )

    debug_groups: List[str] = []
    if markets_matched == 0 and groups:
        debug_groups = _debug_group_lines(groups)

    report_text = render_value_report(result, diagnostics)

    saved, duplicates = _save_recommendations(result.main, storage)

    if owns_storage:
        storage.close()

    return ValuePipelineResult(
        report_text=report_text,
        events_received=len(events),
        events_excluded_by_window=excluded,
        markets_compared=diagnostics.unique_groups,
        candidates_created=len(all_candidates),
        candidates_rejected=len(result.rejected),
        final_recommendations=len(result.main),
        saved_count=saved,
        duplicate_count=duplicates,
        diagnostics=diagnostics,
        debug_groups=debug_groups,
        errors=odds_errors,
    )
