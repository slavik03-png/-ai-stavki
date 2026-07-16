"""
Production v5 orchestration (request-time re-selection, 2026-07-15):
real, currently-quoted Odds API bookmaker coverage GATES which real
API-Football fixtures are worth analysing at all (unchanged from v4),
but the FULL ranked pool of real, odds-backed candidates for the day is
now persisted in the daily archive -- not just a fixed top-5 rendered
once. Every user request re-filters that pool against the CURRENT
moment (excluding fixtures that already started/finished, and any
starting within MIN_LEAD_TIME_MINUTES) and picks the best up-to-5 of
whatever real candidates remain, so a later request automatically picks
up fresh candidates once earlier ones are no longer bettable -- without
spending any additional API-Football/Odds API quota. See
.agents/memory/request-time-reselection.md.

Pipeline:
  discover real fixtures in the strict 36h window (API-Football, 6h-
  cached) -> fetch every currently active real Odds API event once
  (dynamic sport discovery, ai_predictions/odds_client.py) -> match real
  fixtures to real events by team-name + kickoff-time confidence
  (ai_predictions/fixture_matching.py) -> analyse ONLY the matched
  fixtures (up to MAX_FIXTURES_ANALYSED_PER_RUN, soonest kickoff first)
  from API-Football data (statistics/predictions model, never the odds
  themselves) -> rank EVERY fixture that clears the probability
  threshold (not just the top 5), classify HIGH/MEDIUM/LOW -> attach the
  real bookmaker price for each ranked candidate's chosen market from
  the SAME match result (no second fetch) and drop any candidate with no
  real matched price -> persist the resulting pool + re-select the best
  (up to 5) still-startable candidates for right now -> persist those to
  tracking -> render the exact Russian card format.

Why: analysing a fixture no real bookmaker currently quotes always ended
in that candidate being dropped at the very end anyway, wasting
API-Football budget on matches that could never be shown -- and worse,
capping analysis at MAX_FIXTURES_ANALYSED_PER_RUN fixtures picked by
soonest-kickoff-with-no-odds-awareness could exhaust that cap before
ever reaching a fixture that DID have real odds. Gating by real odds
coverage FIRST guarantees every fixture that reaches analysis can
actually become a real, prosentable recommendation, which is what makes
a daily minimum of a few real-odds recommendations achievable.

See .agents/memory/odds-first-fixture-gating.md for the design rationale
of the odds-first gate, and .agents/memory/request-time-reselection.md
for why the daily archive stores a pool instead of a fixed message.
"""

from __future__ import annotations

import datetime
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ai_predictions.fixture_matching import FixtureMatch, match_fixtures_to_events
from ai_predictions.fixtures import Fixture, FixtureDiscoveryResult, discover_fixtures_in_window
from ai_predictions.football_cache import FootballCache
from ai_predictions.football_predictions import MarketCandidate, build_candidates_for_fixture
from ai_predictions.odds_client import QUOTA_EXHAUSTED_MARKER, fetch_all_active_football_events
from ai_predictions.odds_lookup import OddsLookupResult, attach_prices_from_matches
from ai_predictions.prediction_report import (
    render_nothing_left_for_user_message,
    render_pool_empty_message,
    render_predictions_message,
)
from ai_predictions.prediction_selector import (
    RankedRecommendation,
    rank_all_candidates,
    select_current_recommendations,
)
from ai_predictions.value_config import (
    BET_MARKET_LABELS_RU,
    DAILY_ARCHIVE_LOCK_TTL_MINUTES,
    DAILY_ARCHIVE_TTL_HOURS,
    MAX_FIXTURES_ANALYSED_PER_RUN,
    MIN_LEAD_TIME_MINUTES,
    SIGNAL_HIGH,
    SIGNAL_LOW,
    SIGNAL_MEDIUM,
)
from ai_predictions.window import is_same_local_day, local_date_str
from tracking.models import STATUS_PENDING, Prediction
from tracking.storage import DuplicatePredictionError, TrackingStorage

from analytics.integration import record_recommendation
from analytics.storage import AnalyticsStorage

logger = logging.getLogger(__name__)

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

#: Persistent backup of the last archive whose pool contained at least one
#: real odds-backed recommendation.  Never overwritten with an empty pool,
#: never calendar-day-gated -- it survives across midnight and process
#: restarts so that a day when Odds API quota is exhausted can still fall
#: back to yesterday's (or earlier) real picks whose matches haven't yet
#: kicked off.
LAST_SUCCESSFUL_ARCHIVE_KEY = "daily_archive:last_successful_nonempty_v1"

#: Stores the most-recently-seen Odds API credits-remaining header so the
#: next pipeline run can skip the live call when the quota is already known
#: to be zero.  TTL is 26 h (a deliberate margin over 24 h to survive the
#: odds-provider's own daily reset without a gap).
ODDS_CREDITS_CACHE_KEY = "odds_api:credits_remaining_v1"
ODDS_CREDITS_CACHE_TTL_HOURS = 26


@dataclass
class FootballPipelineResult:
    telegram_messages: List[str] = field(default_factory=list)
    found_fixtures: int = 0
    #: Discovered fixtures for which a real, currently-quoted Odds API
    #: event was confidently matched (see fixture_matching.py) -- this is
    #: the odds-first gate: only these are ever passed to analysis.
    matched_fixtures: int = 0
    #: Discovered fixtures with no real odds event matched at all (either
    #: no plausible event, or more than one equally plausible candidate --
    #: never guessed, always excluded honestly).
    unmatched_fixtures_no_odds: int = 0
    analysed_fixtures: int = 0
    fully_stat_fixtures: int = 0
    recommendations_count: int = 0
    api_football_requests_used: int = 0
    api_football_requests_remaining: int = 0
    api_football_requests_used_today: int = 0
    odds_status: str = "unavailable"  # available | quota_exhausted | unavailable
    #: Diagnostics for the Odds API quota-protection requirements (see
    #: .agents/memory/odds-api-quota-protection.md): how many real
    #: sport-competition calls were made to The Odds API during THIS run
    #: (0 when the archive was reused / no key configured), the
    #: x-requests-remaining credits header from the last real call, and
    #: the UTC timestamp of that call -- surfaced verbatim in /status so
    #: an admin can see exactly when quota was last spent, never guessed.
    odds_api_sports_queried: int = 0
    odds_api_credits_remaining: Optional[str] = None
    odds_api_last_request_at: Optional[str] = None
    fixture_discovery: Optional[FixtureDiscoveryResult] = None
    recommendations: List[RankedRecommendation] = field(default_factory=list)
    odds_by_fixture: Dict[int, float] = field(default_factory=dict)
    bookmaker_by_fixture: Dict[int, str] = field(default_factory=dict)
    #: Candidates that cleared the probability threshold but were dropped
    #: because the real, already-matched bookmaker event does not actually
    #: quote that exact market/outcome right now -- never shown, saved or
    #: recorded (see the real-odds gate in run_football_predictions). Rare
    #: under the odds-first architecture since the fixture itself is only
    #: analysed because a real event was already matched to it.
    excluded_no_real_odds_count: int = 0
    saved_count: int = 0
    duplicate_count: int = 0
    errors: List[str] = field(default_factory=list)
    #: The FULL real, odds-backed ranked pool for the day (2026-07-15
    #: change) -- everything that cleared the probability threshold AND
    #: has a real matched bookmaker price, in best-first order, never
    #: sliced to 5. `recommendations`/`telegram_messages` above are only
    #: this run's initial request-time selection from this same pool
    #: (see select_and_render) -- the pool itself is what gets archived,
    #: so a later request can re-select without spending quota again.
    pool: List["PoolEntry"] = field(default_factory=list)


#: One real, odds-backed ranked candidate: the recommendation itself,
#: its real matched bookmaker price, and the bookmaker's own name. This
#: is the unit the daily archive persists -- see module docstring.
PoolEntry = Tuple[RankedRecommendation, float, str]


def _serialize_fixture(fixture: Fixture) -> Dict[str, Any]:
    return {
        "fixture_id": fixture.fixture_id,
        "kickoff_utc": fixture.kickoff_utc.isoformat(),
        "home_team": fixture.home_team,
        "away_team": fixture.away_team,
        "home_team_id": fixture.home_team_id,
        "away_team_id": fixture.away_team_id,
        "league_name": fixture.league_name,
        "league_country": fixture.league_country,
        "status_short": fixture.status_short,
    }


def _deserialize_fixture(data: Dict[str, Any]) -> Fixture:
    return Fixture(
        fixture_id=data["fixture_id"],
        kickoff_utc=datetime.datetime.fromisoformat(data["kickoff_utc"]),
        home_team=data["home_team"],
        away_team=data["away_team"],
        home_team_id=data.get("home_team_id"),
        away_team_id=data.get("away_team_id"),
        league_name=data.get("league_name"),
        league_country=data.get("league_country"),
        status_short=data.get("status_short", ""),
    )


def _serialize_pool_entry(entry: PoolEntry) -> Dict[str, Any]:
    rec, odds, bookmaker = entry
    c = rec.candidate
    return {
        "fixture": _serialize_fixture(c.fixture),
        "market_key": c.market_key,
        "market_label_ru": c.market_label_ru,
        "probability": c.probability,
        "completeness": c.completeness,
        "sample_size_category": c.sample_size_category,
        "rationale": c.rationale,
        "source": c.source,
        "signal_level": rec.signal_level,
        "odds": odds,
        "bookmaker": bookmaker,
    }


def _deserialize_pool_entry(data: Dict[str, Any]) -> PoolEntry:
    candidate = MarketCandidate(
        fixture=_deserialize_fixture(data["fixture"]),
        market_key=data["market_key"],
        market_label_ru=data["market_label_ru"],
        probability=data["probability"],
        completeness=data["completeness"],
        sample_size_category=data["sample_size_category"],
        rationale=data["rationale"],
        source=data["source"],
    )
    rec = RankedRecommendation(candidate=candidate, signal_level=data["signal_level"])
    return rec, data["odds"], data["bookmaker"]


def is_archive_valid(archive: Optional["DailyArchive"]) -> bool:
    """An archive is VALID (has real predictions to offer) only when it
    is present AND its pool has at least one real odds-backed entry.

    An empty pool -- produced when Odds API quota is exhausted, no
    fixtures were found, or every candidate lacked a real bookmaker price
    -- must NOT be treated as a valid archive.  Callers that previously
    checked `archive is not None` would silently fall through to
    `render_nothing_left_for_user_message`, which misleadingly implies
    picks existed and were shown/started when none ever existed."""
    return archive is not None and len(archive.pool) > 0


def archive_empty_reason(diagnostics: Dict[str, Any]) -> str:
    """Human-readable Russian reason why the pool is empty, inferred from
    the diagnostics saved alongside the (empty) archive.  Used by bot.py
    to build an honest user-facing message and by /status."""
    odds_status = diagnostics.get("odds_status", "unavailable")
    if odds_status == "quota_exhausted":
        return "исчерпан лимит The Odds API"
    errors = diagnostics.get("errors", [])
    for err in errors:
        low = err.lower()
        if "лимит" in low and "api-football" in low:
            return "исчерпан лимит API-Football"
        if "quota" in low or "исчерпан" in low:
            return "исчерпан лимит источника данных"
    if diagnostics.get("found_fixtures", 0) == 0:
        return "не найдено матчей в анализируемом окне"
    if diagnostics.get("matched_fixtures", 0) == 0:
        return "ни один матч не сопоставлен с реальными коэффициентами"
    if errors:
        return "ошибка источника данных"
    return "не найдено вариантов с достаточной уверенностью"


def load_last_successful_archive(
    football_cache: FootballCache,
    now: datetime.datetime,
) -> Optional["DailyArchive"]:
    """Returns the most-recent archive whose pool was non-empty, regardless
    of age or calendar day.  Used as a fallback when today's pipeline run
    produced an empty pool: if yesterday's (or an earlier day's) picks
    haven't all kicked off yet, we can still serve them rather than telling
    the user there is nothing at all.

    The returned archive will always have `is_stale_calendar_day=True`
    (since it is by definition not today's run), so bot.py can label it
    clearly as a fallback."""
    payload = football_cache.get(LAST_SUCCESSFUL_ARCHIVE_KEY, ttl_hours=365 * 24)
    if payload is None:
        return None
    try:
        generated_at = datetime.datetime.fromisoformat(payload["generated_at"])
    except (KeyError, ValueError):
        return None
    raw_pool = payload.get("pool", [])
    try:
        pool = [_deserialize_pool_entry(entry) for entry in raw_pool]
    except (KeyError, ValueError, TypeError):
        pool = []
    if not pool:
        return None
    return DailyArchive(
        pool=pool,
        diagnostics=payload.get("diagnostics", {}),
        generated_at=generated_at,
        is_stale_calendar_day=True,  # always true: this is a cross-day backup
    )


def select_and_render(
    pool: List[PoolEntry],
    now: datetime.datetime,
    *,
    found_fixtures: int,
    analysed_fixtures: int,
    matched_fixtures: Optional[int] = None,
    excluded_no_real_odds_count: int = 0,
    min_lead_minutes: float = MIN_LEAD_TIME_MINUTES,
    exclude_keys: Optional[set] = None,
) -> Tuple[List[str], List[PoolEntry]]:
    """The one place request-time re-selection happens (2026-07-15
    change): given the FULL persisted pool for the day and the CURRENT
    moment, excludes anything already started/finished or starting within
    `min_lead_minutes`, keeps the best up-to-5 of whatever real
    candidates remain, and renders the exact card format for them. Pure
    CPU -- no network call, safe to call on every button press and from
    /status-adjacent code paths.

    `exclude_keys` (per-user shown-tracking, 2026-07-15 change): when not
    None, it is a set of (fixture_id, market_key) pairs already shown to
    the REQUESTING user earlier today -- those pool entries are removed
    before ranking/selection, on top of the time-based exclusion above.
    Passing None (the default) means "no per-user tracking requested" and
    preserves the exact prior behaviour/messages for callers that do not
    pass a Telegram user id (e.g. older tests). Passing a set -- even an
    empty one -- means this call IS user-scoped: if nothing real remains
    after both exclusions, the honest "nothing left in today's pool for
    you" message is returned instead of the generic no-signal templates,
    which describe the original run and would be misleading here."""
    ranked = [entry[0] for entry in pool]
    if exclude_keys is not None:
        ranked = [
            r for r in ranked
            if (r.candidate.fixture.fixture_id, r.candidate.market_key) not in exclude_keys
        ]
    selected_recs = select_current_recommendations(ranked, now, min_lead_minutes=min_lead_minutes)
    by_fixture_id = {entry[0].candidate.fixture.fixture_id: entry for entry in pool}
    selected_entries = [by_fixture_id[rec.candidate.fixture.fixture_id] for rec in selected_recs]

    if not selected_recs and exclude_keys is not None:
        return [render_nothing_left_for_user_message()], selected_entries

    odds_and_bookmaker_by_fixture = {
        fid: (odds, bookmaker) for fid, (_, odds, bookmaker) in
        ((entry[0].candidate.fixture.fixture_id, entry) for entry in selected_entries)
    }
    messages = render_predictions_message(
        selected_recs, odds_and_bookmaker_by_fixture,
        found_fixtures=found_fixtures, analysed_fixtures=analysed_fixtures,
        candidates_without_odds=excluded_no_real_odds_count,
        matched_fixtures=matched_fixtures,
    )
    return messages, selected_entries


def _shown_keys_for_user(
    football_cache: Optional[FootballCache],
    telegram_user_id: Optional[int],
    now: datetime.datetime,
) -> Optional[set]:
    """None means "no per-user tracking for this call" (see
    select_and_render's `exclude_keys` docstring) -- only when BOTH a
    football_cache (the shown-picks store lives there) and a real
    Telegram user id are available does this become user-scoped."""
    if football_cache is None or telegram_user_id is None:
        return None
    return football_cache.get_shown_keys(local_date_str(now), telegram_user_id)


def _mark_entries_shown(
    football_cache: Optional[FootballCache],
    telegram_user_id: Optional[int],
    now: datetime.datetime,
    selected_entries: List[PoolEntry],
) -> None:
    if football_cache is None or telegram_user_id is None or not selected_entries:
        return
    keys = [(rec.candidate.fixture.fixture_id, rec.candidate.market_key) for rec, _, _ in selected_entries]
    football_cache.mark_shown(local_date_str(now), telegram_user_id, keys)


def persist_selected(
    selected_entries: List[PoolEntry],
    storage: TrackingStorage,
    analytics_storage: AnalyticsStorage,
    now: datetime.datetime,
) -> Tuple[int, int]:
    """Saves the currently-selected entries to tracking + analytics.
    `storage.save_prediction`'s dedup_key (event_id+market+selection+
    model_version, see tracking/models.py) already guarantees a fixture
    picked again on a later re-selection (because it was still startable)
    is never double-saved -- it simply reports a duplicate, exactly like
    the existing single-run behaviour."""
    archive_version = now.date().isoformat()
    saved, duplicates = 0, 0
    for rec, odds, _bookmaker in selected_entries:
        prediction = _recommendation_to_prediction(rec, odds)
        try:
            storage.save_prediction(prediction)
            saved += 1
        except DuplicatePredictionError:
            duplicates += 1
        # Never allowed to raise or affect the counters above.
        record_recommendation(
            analytics_storage, rec, odds,
            model_version=FOOTBALL_PIPELINE_MODEL_VERSION, archive_version=archive_version, now=now,
        )
    return saved, duplicates


def reselect_from_archive(
    pool: List[PoolEntry],
    diagnostics: Dict[str, Any],
    now: datetime.datetime,
    *,
    storage: Optional[TrackingStorage] = None,
    analytics_storage: Optional[AnalyticsStorage] = None,
    football_cache: Optional[FootballCache] = None,
    telegram_user_id: Optional[int] = None,
) -> Tuple[List[str], List[PoolEntry], int, int]:
    """The entry point bot.py uses on every later button press within
    the same Yekaterinburg calendar day: re-selects from the already
    persisted pool for `now` (no network calls at all) and persists any
    newly-eligible picks to tracking/analytics. Opens its own storage
    here rather than making callers (bot.py in particular) import
    tracking/ directly -- see tests/test_tracking_bot_isolation.py, which
    forbids bot.py from importing tracking at all.

    `football_cache` + `telegram_user_id` (per-user shown-tracking,
    2026-07-15 change): when both are given, this call excludes any pick
    already shown to THIS Telegram user earlier today (on top of the
    existing time-based exclusion) and marks whatever is newly selected
    as shown for them right after. Passing neither preserves the exact
    prior (non-per-user) behaviour."""
    owns_storage = storage is None
    storage = storage or TrackingStorage()
    owns_analytics_storage = analytics_storage is None
    analytics_storage = analytics_storage or AnalyticsStorage(now=now)
    try:
        exclude_keys = _shown_keys_for_user(football_cache, telegram_user_id, now)
        messages, selected_entries = select_and_render(
            pool, now,
            found_fixtures=diagnostics.get("found_fixtures", 0),
            analysed_fixtures=diagnostics.get("analysed_fixtures", 0),
            matched_fixtures=diagnostics.get("matched_fixtures"),
            excluded_no_real_odds_count=diagnostics.get("excluded_no_real_odds_count", 0),
            exclude_keys=exclude_keys,
        )
        saved, duplicates = persist_selected(selected_entries, storage, analytics_storage, now)
        _mark_entries_shown(football_cache, telegram_user_id, now, selected_entries)
        return messages, selected_entries, saved, duplicates
    finally:
        if owns_storage:
            storage.close()
        if owns_analytics_storage:
            analytics_storage.close()


@dataclass
class DailyArchive:
    """The full real, odds-backed candidate pool computed for THIS
    Yekaterinburg calendar day (2026-07-15 change), replayed and
    RE-SELECTED against the current moment on every later button press
    within the same day -- zero API-Football/Odds API calls, but the
    actual recommendations shown can differ from the first request if
    some fixtures are no longer startable (see select_and_render)."""
    pool: List[PoolEntry]
    diagnostics: Dict[str, Any]
    generated_at: datetime.datetime
    is_stale_calendar_day: bool = False


def load_daily_archive(
    football_cache: FootballCache,
    now: datetime.datetime,
    *,
    ignore_ttl: bool = False,
    allow_stale_calendar_day: bool = False,
) -> Optional[DailyArchive]:
    """Returns the persisted daily result, or None if it must not be
    reused. Two independent freshness gates, both logged with the exact
    reason and the dates compared, since either one alone previously let
    a previous day's archive leak into a new day (2026-07-15 fix -- see
    module docstring below):

    1. `ignore_ttl=False` (default): the payload must be at most
       DAILY_ARCHIVE_TTL_HOURS old (rolling hours, existing behaviour).
       `ignore_ttl=True` is used only for the "a refresh is already in
       progress -- fall back to whatever we have" and "verify the write
       just landed on disk" paths.
    2. Calendar-day gate (always enforced unless `allow_stale_calendar_day
       =True`): the archive's `generated_at`, converted to Yekaterinburg
       local time, must fall on the SAME calendar date as `now` converted
       the same way. A rolling 24h TTL alone is not enough -- an archive
       generated at 23:50 Yekaterinburg time yesterday is only 20 minutes
       "old" at 00:10 today, well inside a 24h TTL, but it describes
       yesterday's matches and must never be served as today's result.
       `allow_stale_calendar_day=True` is used only by /status, which
       intentionally reports on a possibly-stale archive for diagnostics
       and never presents it as fresh predictions."""
    ttl = 24.0 * 365 if ignore_ttl else DAILY_ARCHIVE_TTL_HOURS
    payload = football_cache.get(DAILY_ARCHIVE_KEY, ttl_hours=ttl)
    now_date = local_date_str(now)
    if payload is None:
        logger.info(
            "daily_archive.rejected reason=no_payload_or_ttl_expired ttl_hours=%s now_local_date=%s "
            "site=football_pipeline.load_daily_archive",
            ttl, now_date,
        )
        return None
    try:
        generated_at = datetime.datetime.fromisoformat(payload["generated_at"])
    except (KeyError, ValueError):
        logger.warning(
            "daily_archive.rejected reason=unparsable_generated_at now_local_date=%s "
            "site=football_pipeline.load_daily_archive raw=%r",
            now_date, payload.get("generated_at"),
        )
        return None

    archive_date = local_date_str(generated_at)
    same_day = is_same_local_day(generated_at, now)
    if not same_day and not allow_stale_calendar_day:
        logger.info(
            "daily_archive.rejected reason=different_calendar_day archive_local_date=%s now_local_date=%s "
            "generated_at_utc=%s ignore_ttl=%s site=football_pipeline.load_daily_archive",
            archive_date, now_date, generated_at.isoformat(), ignore_ttl,
        )
        return None

    logger.info(
        "daily_archive.accepted reason=%s archive_local_date=%s now_local_date=%s generated_at_utc=%s "
        "ignore_ttl=%s site=football_pipeline.load_daily_archive",
        "same_calendar_day" if same_day else "stale_calendar_day_explicitly_allowed",
        archive_date, now_date, generated_at.isoformat(), ignore_ttl,
    )
    raw_pool = payload.get("pool", [])
    try:
        pool = [_deserialize_pool_entry(entry) for entry in raw_pool]
    except (KeyError, ValueError, TypeError):
        logger.warning(
            "daily_archive.pool_unparsable now_local_date=%s site=football_pipeline.load_daily_archive",
            now_date,
        )
        pool = []
    return DailyArchive(
        pool=pool,
        diagnostics=payload.get("diagnostics", {}),
        generated_at=generated_at,
        is_stale_calendar_day=not same_day,
    )


def save_daily_archive(football_cache: FootballCache, result: "FootballPipelineResult", now: datetime.datetime) -> None:
    pool_is_nonempty = len(result.pool) > 0

    # Never overwrite an existing non-empty today-archive with an empty
    # result (e.g. when the Odds API quota was exhausted during a forced
    # refresh).  The empty diagnostics are still useful for /status, but
    # they must not destroy the real predictions that exist for today.
    if not pool_is_nonempty:
        existing = load_daily_archive(football_cache, now, ignore_ttl=True)
        if is_archive_valid(existing):
            logger.info(
                "daily_archive.save_skipped reason=empty_pool_would_overwrite_valid_archive "
                "now_local_date=%s existing_pool_size=%s site=football_pipeline.save_daily_archive",
                local_date_str(now), len(existing.pool),
            )
            return

    diagnostics = {
        "found_fixtures": result.found_fixtures,
        "matched_fixtures": result.matched_fixtures,
        "unmatched_fixtures_no_odds": result.unmatched_fixtures_no_odds,
        "analysed_fixtures": result.analysed_fixtures,
        "fully_stat_fixtures": result.fully_stat_fixtures,
        "recommendations_count": result.recommendations_count,
        "excluded_no_real_odds_count": result.excluded_no_real_odds_count,
        "api_football_requests_used": result.api_football_requests_used,
        "api_football_requests_remaining": result.api_football_requests_remaining,
        "api_football_requests_used_today": result.api_football_requests_used_today,
        "odds_status": result.odds_status,
        "odds_api_sports_queried": result.odds_api_sports_queried,
        "odds_api_credits_remaining": result.odds_api_credits_remaining,
        "odds_api_last_request_at": result.odds_api_last_request_at,
        "errors": result.errors,
        "source": "новый запрос",
    }
    serialized_pool = [_serialize_pool_entry(entry) for entry in result.pool]
    payload = {
        "pool": serialized_pool,
        "diagnostics": diagnostics,
        "generated_at": now.isoformat(),
    }
    logger.info(
        "daily_archive.saved now_local_date=%s generated_at_utc=%s recommendations_count=%s pool_size=%s "
        "site=football_pipeline.save_daily_archive",
        local_date_str(now), now.isoformat(), result.recommendations_count, len(result.pool),
    )
    football_cache.set(DAILY_ARCHIVE_KEY, payload)

    # Preserve a cross-day backup of the last run that actually produced
    # real picks.  Never overwritten with an empty pool, never expires
    # within 1 year (load_last_successful_archive handles its own TTL).
    if pool_is_nonempty:
        football_cache.set(LAST_SUCCESSFUL_ARCHIVE_KEY, payload)
        logger.info(
            "daily_archive.last_successful_saved now_local_date=%s pool_size=%s "
            "site=football_pipeline.save_daily_archive",
            local_date_str(now), len(result.pool),
        )


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


def _recommendation_to_prediction(rec: RankedRecommendation, odds: float) -> Prediction:
    """`odds` must be a real, confirmed bookmaker price -- callers only
    ever reach this for recommendations that survived the real-odds
    filter in run_football_predictions (a recommendation with no real
    price is dropped before persistence, never given a fabricated
    implied-probability price here)."""
    c = rec.candidate
    fixture = c.fixture
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
        bookmaker_odds=odds,
        model_probability=c.probability,
        confidence_score=round(c.probability * 100.0, 1),
        confidence_level=c.sample_size_category,
        recommendation_group=_LEVEL_TO_RECOMMENDATION_GROUP.get(rec.signal_level, "high_risk"),
        explanation=explanation,
        data_provider="api_football+the_odds_api",
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
    analytics_storage: Optional[AnalyticsStorage] = None,
    telegram_user_id: Optional[int] = None,
) -> FootballPipelineResult:
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

    result = FootballPipelineResult()

    fixture_discovery = discover_fixtures_in_window(football_api_key, football_cache, now)
    result.fixture_discovery = fixture_discovery
    result.found_fixtures = len(fixture_discovery.fixtures)
    result.errors.extend(fixture_discovery.errors)

    # --- Odds-first gate: fetch every real, currently active Odds API
    # event ONCE, and match it to the real discovered fixtures ONCE. Only
    # fixtures with a confident match are ever worth spending API-Football
    # budget analysing -- see module docstring. The exact same match
    # result is reused at the very end to price the final picks, so this
    # is the only Odds API fetch for the whole run.
    if not odds_api_key:
        # Deliberately short-circuits BEFORE calling fetch_all_active_football_events:
        # that function (and discover_football_sport_keys underneath it)
        # falls back to os.getenv("ODDS_API_KEY") for a falsy api_key, which
        # would silently use the real environment secret even when a caller
        # explicitly passed no key (e.g. a test simulating "no key
        # configured"). An explicit empty/None odds_api_key must mean
        # "definitely no odds", never "fall back to whatever's in the
        # environment".
        odds_fetch_errors = ["Не найден ODDS_API_KEY"]
        odds_events = []
    else:
        # Quota guard: if the last known credits value is "0" we know the
        # Odds API will reject any new request until its daily counter
        # resets.  Skip the live call entirely rather than spending a
        # request (and potentially getting throttled) to hear the same
        # answer we already have cached.
        cached_credits = football_cache.get(ODDS_CREDITS_CACHE_KEY, ttl_hours=ODDS_CREDITS_CACHE_TTL_HOURS)
        if cached_credits is not None and cached_credits.get("remaining") == "0":
            logger.info(
                "odds_api.skipped reason=quota_known_zero cached_remaining=0 "
                "site=football_pipeline.run_football_predictions"
            )
            odds_fetch_errors = [
                f"{QUOTA_EXHAUSTED_MARKER}: квота The Odds API уже исчерпана "
                f"(остаток 0 по последнему ответу API, повторный запрос пропущен)"
            ]
            odds_events = []
            result.odds_api_credits_remaining = "0"
        else:
            try:
                odds_fetch = fetch_all_active_football_events(api_key=odds_api_key, persistent_cache=football_cache)
                odds_fetch_errors = odds_fetch.errors
                odds_events = odds_fetch.events
                # This call is the ONLY place the whole pipeline spends real
                # Odds API quota (see module docstring + odds-first gating
                # memory) -- record exactly how much was spent and when, so
                # /status can show real numbers instead of a vague tri-state.
                result.odds_api_sports_queried = len(odds_fetch.sports_queried)
                result.odds_api_credits_remaining = odds_fetch.credits_remaining
                result.odds_api_last_request_at = now.isoformat()
                # Persist the latest credits count so the NEXT run can
                # short-circuit without spending quota (see above).
                if odds_fetch.credits_remaining is not None:
                    football_cache.set(
                        ODDS_CREDITS_CACHE_KEY,
                        {"remaining": odds_fetch.credits_remaining},
                    )
            except Exception as exc:  # never let odds discovery crash the run
                odds_fetch_errors = [str(exc)]
                odds_events = []

    if any(QUOTA_EXHAUSTED_MARKER in e for e in odds_fetch_errors):
        result.odds_status = "quota_exhausted"
    elif not odds_api_key or (odds_fetch_errors and not odds_events):
        result.odds_status = "unavailable"
    else:
        result.odds_status = "available"

    match_result = match_fixtures_to_events(fixture_discovery.fixtures, odds_events)
    result.matched_fixtures = len(match_result.matches)
    result.unmatched_fixtures_no_odds = len(match_result.unmatched_fixtures) + len(match_result.ambiguous_fixtures)

    # Soonest kickoff first among the matched pool only -- unmatched
    # fixtures (no real bookmaker coverage right now) are never analysed
    # at all, so the analysis cap can never be exhausted by fixtures that
    # could never become a real recommendation anyway.
    fixtures_with_real_odds = sorted((m.fixture for m in match_result.matches), key=lambda f: f.kickoff_utc)

    from football.providers.api_football import ApiFootballProvider
    provider = ApiFootballProvider(api_key=football_api_key, now=now)

    # Every matched fixture up to max_fixtures_analysed is always
    # analysed -- build_candidates_for_fixture never needs to be skipped
    # wholesale: it reads persistent cache first, only spends real
    # requests while football_cache.can_spend(1) allows it (per real HTTP
    # call, not per fixture), and falls back to a historical-baseline
    # signal when nothing real is available at all. This guarantees
    # analysed_fixtures is never 0 while matched_fixtures > 0 -- the
    # daily quota reserve can only reduce *how much real data* backs each
    # candidate, never how many matched fixtures get ranked.
    all_candidates: List[MarketCandidate] = []
    analysed = 0
    fully_stat_fixture_ids: set = set()
    quota_exhausted_during_run = False
    for fixture in fixtures_with_real_odds[:max_fixtures_analysed]:
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

    # Rank EVERY analysed fixture that clears the probability threshold --
    # deliberately NOT sliced to 5 here (2026-07-15 change): the daily
    # archive persists this whole pool so a later request, once some of
    # today's picks have already kicked off, can still re-select fresh
    # real candidates from what remains without spending quota again.
    full_ranked = rank_all_candidates(all_candidates)

    fixture_market_keys = {
        rec.candidate.fixture.fixture_id: rec.candidate.market_key for rec in full_ranked
    }
    try:
        odds_result = attach_prices_from_matches(match_result.matches, fixture_market_keys)
    except Exception as exc:  # never let price attachment break the run
        odds_result = OddsLookupResult(prices_by_fixture={}, status="unavailable", detail=str(exc))
    result.odds_by_fixture = odds_result.prices_by_fixture
    result.bookmaker_by_fixture = odds_result.bookmaker_by_fixture

    # Mandatory real-odds gate: a candidate only ever enters the pool (and
    # can therefore ever be shown, saved or recorded) when a real
    # bookmaker actually quotes that exact event and market right now. No
    # placeholder/estimated coefficient is ever substituted -- a candidate
    # with no real, matched price is dropped here, before it ever reaches
    # the pool, rendering, storage or analytics.
    pool: List[PoolEntry] = [
        (rec, odds_result.prices_by_fixture[fid], odds_result.bookmaker_by_fixture.get(fid, "?"))
        for rec in full_ranked
        if (fid := rec.candidate.fixture.fixture_id) in odds_result.prices_by_fixture
    ]
    result.pool = pool
    result.excluded_no_real_odds_count = len(full_ranked) - len(pool)
    if result.excluded_no_real_odds_count:
        logger.info(
            "football_pipeline.recommendations_excluded_no_real_odds count=%d "
            "(candidates that passed the probability threshold but had no real, "
            "matched bookmaker price -- dropped rather than shown with a placeholder)",
            result.excluded_no_real_odds_count,
        )

    # This run's own initial request-time selection from the freshly
    # built pool -- exactly the same re-selection logic a later button
    # press against the archived pool will use (see select_and_render).
    exclude_keys = _shown_keys_for_user(football_cache, telegram_user_id, now)
    messages, selected_entries = select_and_render(
        pool, now,
        found_fixtures=result.found_fixtures, analysed_fixtures=result.analysed_fixtures,
        matched_fixtures=result.matched_fixtures,
        excluded_no_real_odds_count=result.excluded_no_real_odds_count,
        exclude_keys=exclude_keys,
    )
    result.telegram_messages = messages
    result.recommendations = [entry[0] for entry in selected_entries]
    result.recommendations_count = len(selected_entries)

    saved, duplicates = persist_selected(selected_entries, storage, analytics_storage, now)
    result.saved_count = saved
    result.duplicate_count = duplicates
    _mark_entries_shown(football_cache, telegram_user_id, now, selected_entries)

    if owns_storage:
        storage.close()
    if owns_football_cache:
        football_cache.close()
    if owns_analytics_storage:
        analytics_storage.close()

    return result
