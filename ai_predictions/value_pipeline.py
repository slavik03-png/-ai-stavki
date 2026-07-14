"""
End-to-end orchestration for the fixture-discovery-first, ranked
HIGH/MEDIUM/LOW/REJECTED cross-bookmaker value-detection strategy:

  discover every real fixture kicking off in the strict 36h window
  (Asia/Yekaterinburg, [now, now+36h)) from API-Football
  (ai_predictions/fixtures.py) -> scope The Odds API querying to only the
  sport_keys plausibly matching those fixtures' leagues/countries
  (ai_predictions/league_relevance.py) -> fetch real odds for that scoped
  set, quota-safely 24h-cached (ai_predictions/odds_client.py) -> confident
  fixture<->event matching, ambiguous pairs dropped
  (ai_predictions/fixture_matching.py) -> extract/validate/dedupe/group real
  bookmaker rows for matched events only (ai_predictions/matching.py) ->
  build real ValueCandidates with a signal_level + ranking_score from
  cross-bookmaker price divergence (ai_predictions/value_engine.py) ->
  fixture-aware statistics enrichment + auditable market+statistics
  probability blend, which can move a candidate between tiers
  (ai_predictions/enrichment.py, ai_predictions/probability_model.py) ->
  rank ALL candidates globally (HIGH -> MEDIUM -> LOW) and keep up to 5
  total, filling down tiers rather than ever showing REJECTED
  (ai_predictions/value_selector.py) -> persist EVERY evaluated candidate
  (including REJECTED ones) to tracking, dedup-safe -> render the Russian
  report text with full pipeline and fixture-discovery diagnostics.
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ai_predictions.enrichment import FixtureEnrichmentSummary, enrich_matched_candidates
from ai_predictions.fixture_matching import FixtureMatchResult, match_fixtures_to_events
from ai_predictions.fixtures import Fixture, FixtureDiscoveryResult, discover_fixtures_in_window
from ai_predictions.football_cache import FootballCache
from ai_predictions.league_relevance import select_relevant_sport_keys
from ai_predictions.matching import (
    MarketGroup,
    ValidationStats,
    dedupe_bookmaker_rows,
    extract_rows,
    group_rows,
    raw_bookmaker_row_counts,
    validate_rows,
)
from ai_predictions.odds_client import (
    QUOTA_EXHAUSTED_MARKER,
    STALE_ODDS_MARKER,
    discover_football_sport_keys,
    fetch_active_sports,
    fetch_football_events,
)
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

ODDS_CACHE_DB_PATH = os.path.join("data", "odds_api_cache.db")

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
    #: Fixture-aware enrichment summary for this run (see
    #: ai_predictions.enrichment.FixtureEnrichmentSummary) -- always
    #: present, even when enrichment made zero real requests.
    enrichment_summary: Optional[FixtureEnrichmentSummary] = None
    fixture_discovery: Optional[FixtureDiscoveryResult] = None
    fixture_match_result: Optional[FixtureMatchResult] = None


def _event_id(event: Dict[str, Any]) -> str:
    return str(event.get("id") or f"{event.get('_sport_key')}|{event.get('commence_time')}|{event.get('home_team')}|{event.get('away_team')}")


def _candidate_to_prediction(candidate: ValueCandidate) -> Prediction:
    outlier_flag = candidate.outlier_warning is not None
    rejection_text = "; ".join(candidate.rejection_reasons) if candidate.rejection_reasons else None
    model_probability = candidate.estimated_probability if candidate.estimated_probability is not None else candidate.consensus_probability
    edge = candidate.edge_final if candidate.edge_final is not None else candidate.edge
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
        model_probability=model_probability,
        confidence_score=round(max(0.0, min(100.0, edge * 100.0)), 1),
        confidence_level=_confidence_level(candidate.unique_bookmaker_count),
        recommendation_group=_LEVEL_TO_RECOMMENDATION_GROUP.get(candidate.signal_level, "avoid"),
        explanation=(
            f"Лучшая цена {candidate.best_price:.2f} у {candidate.best_bookmaker} против справедливой "
            f"{candidate.fair_price:.2f} по {candidate.consensus_bookmaker_count} другим букмекерам "
            f"(расхождение {edge:.3f}, всего {candidate.unique_bookmaker_count} букмекеров по исходу)."
        ),
        data_provider="the_odds_api+api_football" if candidate.fixture_id else "the_odds_api",
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
        fixture_id=candidate.fixture_id,
        matching_confidence=candidate.fixture_match_confidence,
        sample_size_category=candidate.sample_size_category,
        market_probability=candidate.market_probability,
        statistics_probability=candidate.statistics_probability,
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


_UNSET = object()


def run_value_predictions(
    *,
    odds_api_key: Optional[str] = None,
    football_api_key: Any = _UNSET,
    storage: Optional[TrackingStorage] = None,
    now: Optional[datetime.datetime] = None,
    max_events: Optional[int] = None,
) -> ValuePipelineResult:
    """Runs the full fixture-discovery-first value-detection pipeline once.
    Never raises for expected failure modes (no fixtures, no odds, no
    match, no qualifying divergence) -- those show up as an honest "no
    recommendations today" report, with full diagnostics distinguishing
    each condition from the others."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    owns_storage = storage is None
    storage = storage or TrackingStorage()
    odds_api_key = odds_api_key or os.getenv("ODDS_API_KEY")
    if football_api_key is _UNSET:
        football_api_key = os.getenv("FOOTBALL_API_KEY")

    football_cache = FootballCache(now=now)
    odds_cache = FootballCache(db_path=ODDS_CACHE_DB_PATH, now=now)

    # -- Phase 1: real fixture discovery (API-Football, primary source of
    #    truth for "what matches actually exist in the window"). --
    fixture_discovery = discover_fixtures_in_window(football_api_key, football_cache, now)
    fixtures = fixture_discovery.fixtures

    odds_errors: List[str] = list(fixture_discovery.errors)
    sports_skipped: Dict[str, str] = {}
    events: List[Dict[str, Any]] = []
    credits_remaining = None
    discovery_source = "api"
    discovery_error = None
    quota_exhausted = False
    stale_odds = False
    scoped_sport_keys: List[str] = []
    all_active_football: List[str] = []

    if not odds_api_key:
        odds_errors.append("Не найден ODDS_API_KEY")
    elif not fixtures:
        # Nothing to scope The Odds API querying to -- querying it blindly
        # would burn quota for events we could never confidently match to
        # a real fixture anyway.
        pass
    else:
        catalog, catalog_error = fetch_active_sports(odds_api_key, odds_cache)
        if catalog_error and STALE_ODDS_MARKER in catalog_error:
            stale_odds = True
        if catalog_error and QUOTA_EXHAUSTED_MARKER in catalog_error and not catalog:
            quota_exhausted = True
        catalog = catalog or []
        football_catalog = [c for c in catalog if c.get("group") == "Soccer"]
        all_active_football = [c["key"] for c in football_catalog if c.get("key")]
        scoped_sport_keys = select_relevant_sport_keys(fixtures, football_catalog)

        if scoped_sport_keys:
            odds_events, credits_remaining, fetch_errors = fetch_football_events(
                api_key=odds_api_key, sport_keys=scoped_sport_keys, persistent_cache=odds_cache,
            )
            events = odds_events
            odds_errors.extend(fetch_errors)
            for err in fetch_errors:
                if QUOTA_EXHAUSTED_MARKER in err:
                    quota_exhausted = True
                if STALE_ODDS_MARKER in err:
                    stale_odds = True
        else:
            odds_errors.append(
                "Ни один вид спорта The Odds API не сопоставлен с обнаруженными турнирами/странами фикстур"
            )

    in_window, excluded = filter_events_in_window(events, now)
    if max_events is not None:
        in_window = in_window[:max_events]

    # -- Phase 4: confident fixture<->event matching; ambiguous pairs are
    #    dropped rather than guessed. --
    match_result = match_fixtures_to_events(fixtures, in_window)
    matched_events = [m.event for m in match_result.matches]
    fixtures_by_event_id: Dict[str, Fixture] = {}
    match_confidence_by_event_id: Dict[str, float] = {}
    for m in match_result.matches:
        event_id = _event_id(m.event)
        fixtures_by_event_id[event_id] = m.fixture
        match_confidence_by_event_id[event_id] = m.confidence

    stats = ValidationStats()
    all_rows = []
    for event in matched_events:
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
    for c in all_candidates:
        conf = match_confidence_by_event_id.get(c.event_id)
        if conf is not None:
            c.fixture_match_confidence = conf

    # Phase 5+6: fixture-aware statistics enrichment + auditable
    # market+statistics probability blend -- can move a candidate between
    # HIGH/MEDIUM/LOW/REJECTED (see value_engine.apply_probability_blend),
    # unlike the older odds-only compute_combined_score nudge.
    enrichment_summary = enrich_matched_candidates(
        all_candidates,
        fixtures_by_event_id,
        api_key=football_api_key,
        cache=football_cache,
        now=now,
    )

    result = select_value_recommendations(all_candidates)

    outlier_warning_count = sum(1 for c in all_candidates if c.outlier_warning)
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
        sports_discovered=all_active_football,
        sports_queried=scoped_sport_keys,
        sports_skipped=sports_skipped,
        discovery_source=discovery_source,
        discovery_error=discovery_error,
        api_football_attempted_events=enrichment_summary.attempted_events,
        api_football_matched_events=enrichment_summary.blended_events,
        api_football_unmatched_events=enrichment_summary.market_only_events,
        api_football_requests_used=enrichment_summary.api_football_requests_used,
        api_football_quota_remaining_today=enrichment_summary.api_football_quota_remaining_today,
        api_football_season_allowed=True,
        api_football_skipped_reason=enrichment_summary.skipped_reason,
        fixtures_discovered=len(fixtures),
        fixtures_excluded_by_window=fixture_discovery.excluded_by_window,
        fixtures_excluded_by_status=fixture_discovery.excluded_by_status,
        fixtures_matched_to_odds=len(match_result.matches),
        fixtures_unmatched=len(match_result.unmatched_fixtures),
        fixtures_ambiguous=len(match_result.ambiguous_fixtures),
        odds_events_unmatched=len(match_result.unmatched_events),
        odds_quota_exhausted=quota_exhausted,
        odds_stale_fallback=stale_odds,
    )

    debug_groups: List[str] = []
    if markets_matched == 0 and groups:
        debug_groups = _debug_group_lines(groups)

    report_text = render_value_report(result, diagnostics)
    telegram_messages = render_telegram_signals_message(result, diagnostics)
    api_error_summary = summarize_api_errors_ru(odds_errors, {})

    saved, duplicates = _save_all_candidates(all_candidates, storage)

    if owns_storage:
        storage.close()
    football_cache.close()
    odds_cache.close()

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
        sports_discovered=all_active_football,
        sports_queried=scoped_sport_keys,
        sports_skipped=sports_skipped,
        enrichment_summary=enrichment_summary,
        fixture_discovery=fixture_discovery,
        fixture_match_result=match_result,
    )
