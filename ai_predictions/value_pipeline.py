"""
End-to-end orchestration for the ranked HIGH/MEDIUM/LOW/REJECTED
cross-bookmaker value-detection strategy:

  discover every active football competition from The Odds API (not a
  hardcoded major-league list) -> fetch real odds for all of them -> 36h
  window filter -> extract/validate/dedupe/group real bookmaker rows
  (ai_predictions/matching.py) -> build real ValueCandidates with a
  signal_level + ranking_score from cross-bookmaker price divergence
  (ai_predictions/value_engine.py) -> rank ALL candidates globally
  (HIGH -> MEDIUM -> LOW) and keep up to 5 total, one signal per event
  with limited exceptions (ai_predictions/value_selector.py) -> persist
  EVERY evaluated candidate (including REJECTED ones) to tracking,
  dedup-safe -> render the Russian report text with full pipeline and
  event-discovery diagnostics.

Deliberately does not call football/ or ApiFootballProvider at all: this
strategy needs no team statistics. The API-Football integration
(football/providers/api_football.py) is left completely untouched and
available for ai_predictions/pipeline.py (the statistics-based strategy)
once a paid plan makes current-season data available.
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ai_predictions.enrichment import EnrichmentSummary, enrich_candidates
from ai_predictions.football_cache import FootballCache
from ai_predictions.matching import (
    MarketGroup,
    ValidationStats,
    dedupe_bookmaker_rows,
    extract_rows,
    group_rows,
    raw_bookmaker_row_counts,
    validate_rows,
)
from ai_predictions.odds_client import fetch_all_active_football_events
from ai_predictions.value_config import (
    LOW_MIN_BOOKMAKERS,
    MODEL_VERSION,
    SIGNAL_HIGH,
    SIGNAL_LOW,
    SIGNAL_MEDIUM,
    SIGNAL_REJECTED,
)
from ai_predictions.value_engine import ValueCandidate, build_value_candidates_from_groups
from ai_predictions.value_report import (
    Diagnostics,
    compute_top_rejection_reasons,
    render_telegram_signals_message,
    render_value_report,
    summarize_api_errors_ru,
)
from ai_predictions.value_selector import ValueSelectionResult, select_value_recommendations
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

#: recommendation_group is a legacy tracking column (validated against a
#: fixed set) reused here purely as a coarse bucket so existing
#: statistics.by_recommendation_group breakdowns keep working; the
#: authoritative ranked level lives in Prediction.signal_level.
_LEVEL_TO_RECOMMENDATION_GROUP = {
    SIGNAL_HIGH: "main",
    SIGNAL_MEDIUM: "alternative",
    SIGNAL_LOW: "high_risk",
    SIGNAL_REJECTED: "avoid",
}


def _confidence_level(bookmaker_count: int) -> str:
    """A confidence_level bucket derived purely from real, observable
    market agreement (bookmaker_count) -- explicitly NOT a statistical
    confidence claim about the match outcome, only about how many
    independent real bookmakers back the divergence."""
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
    #: Concise, Russian, non-technical message(s) for the "🤖 Прогнозы ИИ"
    #: button -- one or more chunks, never exceeding Telegram's message
    #: length limit. Contains only the ranked signal cards + a short count
    #: summary; all raw diagnostics stay in report_text for /status.
    telegram_messages: List[str] = field(default_factory=list)
    #: One short Russian line summarizing any per-competition API errors
    #: (e.g. "Некоторые турниры недоступны: HTTP 401 — 24 турнира."), for
    #: /status only. None when nothing failed.
    api_error_summary: Optional[str] = None
    diagnostics: Optional[Diagnostics] = None
    debug_groups: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    sports_discovered: List[str] = field(default_factory=list)
    sports_queried: List[str] = field(default_factory=list)
    sports_skipped: Dict[str, str] = field(default_factory=dict)
    #: API-Football enrichment summary for this run (see
    #: ai_predictions.enrichment.EnrichmentSummary) -- always present, even
    #: when enrichment made zero real requests.
    enrichment_summary: Optional[EnrichmentSummary] = None


def _event_id(event: Dict[str, Any]) -> str:
    return str(event.get("id") or f"{event.get('_sport_key')}|{event.get('commence_time')}|{event.get('home_team')}|{event.get('away_team')}")


def _candidate_to_prediction(candidate: ValueCandidate) -> Prediction:
    outlier_flag = candidate.outlier_warning is not None
    rejection_text = "; ".join(candidate.rejection_reasons) if candidate.rejection_reasons else None
    return Prediction(
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
        confidence_level=_confidence_level(candidate.unique_bookmaker_count),
        recommendation_group=_LEVEL_TO_RECOMMENDATION_GROUP.get(candidate.signal_level, "avoid"),
        explanation=(
            f"Лучшая цена {candidate.best_price:.2f} у {candidate.best_bookmaker} против справедливой "
            f"{candidate.fair_price:.2f} по {candidate.consensus_bookmaker_count} другим букмекерам "
            f"(расхождение {candidate.edge:.3f}, всего {candidate.unique_bookmaker_count} букмекеров по исходу)."
        ),
        data_provider="the_odds_api",
        model_version=MODEL_VERSION,
        line=candidate.line,
        status=STATUS_PENDING,
        signal_level=candidate.signal_level,
        ranking_score=candidate.ranking_score,
        outlier_warning=outlier_flag,
        rejection_reason=rejection_text,
        statistics_source=candidate.statistics_source,
        statistics_cached=candidate.statistics_cached,
        statistics_completeness=candidate.statistics_completeness,
        statistics_score=candidate.statistics_score,
        final_combined_score=candidate.final_combined_score,
    )


def _save_all_candidates(candidates: List[ValueCandidate], storage: TrackingStorage) -> "tuple[int, int]":
    """Persists EVERY evaluated candidate this run -- HIGH, MEDIUM, LOW and
    REJECTED alike -- so later statistics can measure each level's real
    settled performance, not just the ones shown to the user. Never
    overwrites an older model version's rows: dedup_key includes
    model_version, so bumping MODEL_VERSION always creates fresh rows."""
    saved, duplicates = 0, 0
    for candidate in candidates:
        prediction = _candidate_to_prediction(candidate)
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

    fetch_result = fetch_all_active_football_events(api_key=odds_api_key)
    events = fetch_result.events
    credits_remaining = fetch_result.credits_remaining
    odds_errors = list(fetch_result.errors)
    discovery = fetch_result.discovery
    sports_skipped: Dict[str, str] = dict(discovery.skipped)
    sports_skipped.update(fetch_result.sports_failed)

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
    raw_counts = raw_bookmaker_row_counts(valid_rows)
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
        if max_bm >= LOW_MIN_BOOKMAKERS:
            markets_matched += 1

    all_candidates = build_value_candidates_from_groups(groups, raw_counts)

    # API-Football statistics enrichment: takes the top preliminary
    # candidates (pure odds-only ranking_score) and, best-effort, attaches
    # a real recent-form statistics signal that only re-ranks WITHIN an
    # already-decided HIGH/MEDIUM/LOW tier (see value_engine.compute_
    # combined_score) -- never changes signal_level, never invents a
    # market, never raises. select_value_recommendations below then uses
    # the (possibly stats-nudged) score for its own within-tier ordering.
    enrichment_summary = enrich_candidates(
        all_candidates,
        api_key=os.getenv("FOOTBALL_API_KEY"),
        cache=FootballCache(now=now),
        now=now,
    )

    result = select_value_recommendations(all_candidates)

    outlier_warning_count = sum(1 for c in all_candidates if c.outlier_warning)
    # Real total counts per level across EVERY evaluated candidate this
    # run (Step 12) -- deliberately independent of how many made it into
    # the displayed top_signals list, which is capped at MAX_TOTAL_SIGNALS.
    high_total = sum(1 for c in all_candidates if c.signal_level == SIGNAL_HIGH)
    medium_total = sum(1 for c in all_candidates if c.signal_level == SIGNAL_MEDIUM)
    low_total = sum(1 for c in all_candidates if c.signal_level == SIGNAL_LOW)
    rejected_total = sum(1 for c in all_candidates if c.signal_level == SIGNAL_REJECTED)

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
        candidates_rejected=rejected_total,
        final_recommendations=len(result.top_signals),
        duplicate_bookmaker_rows=stats.duplicate_bookmaker_rows,
        unsupported_markets_seen=dict(stats.unsupported_markets_seen),
        top_rejection_reasons=compute_top_rejection_reasons(result.rejected),
        high_count=high_total,
        medium_count=medium_total,
        low_count=low_total,
        rejected_count=rejected_total,
        outlier_warning_count=outlier_warning_count,
        remaining_odds_api_credits=credits_remaining,
        sports_discovered=discovery.all_active_football,
        sports_queried=fetch_result.sports_queried,
        sports_skipped=sports_skipped,
        discovery_source=discovery.source,
        discovery_error=discovery.discovery_error,
        api_football_attempted_events=enrichment_summary.attempted_events,
        api_football_matched_events=enrichment_summary.matched_events,
        api_football_unmatched_events=enrichment_summary.unmatched_events,
        api_football_requests_used=enrichment_summary.api_football_requests_used,
        api_football_quota_remaining_today=enrichment_summary.api_football_quota_remaining_today,
        api_football_season_allowed=enrichment_summary.season_allowed,
        api_football_skipped_reason=enrichment_summary.skipped_reason,
    )

    debug_groups: List[str] = []
    if markets_matched == 0 and groups:
        debug_groups = _debug_group_lines(groups)

    report_text = render_value_report(result, diagnostics)
    telegram_messages = render_telegram_signals_message(result, diagnostics)
    # Only real fetch failures count as "API errors" here -- normal
    # discovery skips (inactive competition, outrights-only market) are
    # expected filtering, not an error, and must never be aggregated as
    # one. odds_errors already carries exactly one entry per genuinely
    # failed competition (see fetch_football_events), so sports_failed
    # (a same-source lookup keyed by sport, used only for sports_queried)
    # is deliberately NOT also passed here to avoid double-counting.
    api_error_summary = summarize_api_errors_ru(odds_errors, {})

    saved, duplicates = _save_all_candidates(all_candidates, storage)

    if owns_storage:
        storage.close()

    return ValuePipelineResult(
        report_text=report_text,
        events_received=len(events),
        events_excluded_by_window=excluded,
        markets_compared=diagnostics.unique_groups,
        candidates_created=len(all_candidates),
        candidates_rejected=rejected_total,
        final_recommendations=len(result.top_signals),
        saved_count=saved,
        duplicate_count=duplicates,
        telegram_messages=telegram_messages,
        api_error_summary=api_error_summary,
        diagnostics=diagnostics,
        debug_groups=debug_groups,
        errors=odds_errors,
        high_count=high_total,
        medium_count=medium_total,
        low_count=low_total,
        sports_discovered=discovery.all_active_football,
        sports_queried=fetch_result.sports_queried,
        sports_skipped=sports_skipped,
        enrichment_summary=enrichment_summary,
    )
