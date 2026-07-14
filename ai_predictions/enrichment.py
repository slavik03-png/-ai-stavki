"""
API-Football statistics enrichment step for the value-divergence
strategy: takes the top preliminary ValueCandidates (already scored purely
from real bookmaker prices), tries to attach a real recent-form statistics
signal to each one via API-Football, and blends it into a
final_combined_score used only to re-rank within an already-decided
HIGH/MEDIUM/LOW tier (never to change the tier itself).

Runs strictly best-effort: any failure (no API key, unmatched team, no
quota left, provider error, structurally unavailable free-plan data) marks
the affected candidates' statistics_source honestly and leaves their
odds-only ranking_score as the final answer -- this step never raises and
never blocks the odds-only pipeline.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ai_predictions.football_cache import FootballCache
from ai_predictions.football_matching import TeamMatch, best_team_match
from ai_predictions.fixtures import Fixture
from ai_predictions.probability_model import blend_probability, statistics_probability_for_side
from ai_predictions.value_config import (
    API_FOOTBALL_FREE_PLAN_SEASONS,
    ENRICHMENT_SHORTLIST_SIZE,
)
from ai_predictions.value_engine import ValueCandidate, apply_probability_blend, compute_combined_score
from ai_predictions.matching import AWAY, HOME
from football.interface import Stat
from football.providers.api_football import ApiFootballProvider, _season_for


@dataclass
class EnrichmentSummary:
    attempted_events: int = 0
    matched_events: int = 0
    unmatched_events: int = 0
    api_football_requests_used: int = 0
    api_football_quota_remaining_today: Optional[int] = None
    season_allowed: bool = False
    skipped_reason: Optional[str] = None
    per_event_source: Dict[str, str] = field(default_factory=dict)


def _event_key(candidate: ValueCandidate) -> Tuple[str, str, str]:
    return (candidate.event_id, candidate.home_team, candidate.away_team)


def _win_rate(form_letters: str) -> Optional[float]:
    if not form_letters:
        return None
    wins = form_letters.count("W")
    return wins / len(form_letters)


def _team_cache_key(team_id: int, endpoint: str) -> str:
    return f"team:{team_id}:{endpoint}"


def _resolve_and_cache_team(
    provider: ApiFootballProvider, cache: FootballCache, team_name: str
) -> "tuple[Optional[TeamMatch], int]":
    """Resolves one real team via the cache first, falling back to a real
    /teams search only on a cache miss. Returns (match_or_None, requests_spent)."""
    cache_key = f"resolve:{team_name.strip().lower()}"
    cached = cache.get(cache_key)
    if cached is not None:
        if not cached.get("matched"):
            return TeamMatch(matched=False, reason=cached.get("reason")), 0
        return (
            TeamMatch(
                matched=True,
                team_id=cached.get("team_id"),
                matched_name=cached.get("matched_name"),
                country=cached.get("country"),
                confidence=cached.get("confidence", 0.0),
            ),
            0,
        )

    stat: Stat = provider.search_teams(team_name)
    if not stat.available:
        # Transient (rate limit / network / quota) -- never cached, a
        # later run within the same day may still succeed.
        return None, 1

    match = best_team_match(team_name, stat.value)
    cache.set(cache_key, {
        "matched": match.matched,
        "team_id": match.team_id,
        "matched_name": match.matched_name,
        "country": match.country,
        "confidence": match.confidence,
        "reason": match.reason,
    })
    return match, 1


def _team_form_win_rate(
    provider: ApiFootballProvider, cache: FootballCache, team_id: int, matched_name: str
) -> "tuple[Optional[float], int]":
    """Real recent-form win rate for one already-matched team, via cache
    first. Returns (win_rate_or_None, requests_spent)."""
    cache_key = _team_cache_key(team_id, "home_away_form")
    cached = cache.get(cache_key)
    if cached is not None:
        return cached.get("win_rate"), 0

    stat = provider.get_home_away_form(matched_name)
    if not stat.available:
        # Free-plan season/params restrictions are a structural
        # "no data available", worth caching so this run and later runs
        # today don't re-spend quota re-discovering the same restriction.
        cache.set(cache_key, {"win_rate": None})
        return None, 1

    win_rate = _win_rate(stat.value.overall)
    cache.set(cache_key, {"win_rate": win_rate})
    return win_rate, 1


def enrich_candidates(
    candidates: List[ValueCandidate],
    *,
    api_key: Optional[str],
    cache: Optional[FootballCache] = None,
    now: Optional[datetime.datetime] = None,
    max_requests: Optional[int] = None,
) -> EnrichmentSummary:
    """Enriches up to ENRICHMENT_SHORTLIST_SIZE preliminary candidates
    (already ranked by ranking_score) with a real API-Football statistics
    signal, mutating each candidate's statistics_* fields and
    final_combined_score in place. Always returns a summary describing
    exactly what happened -- real request counts, real match counts,
    never invented."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    owns_cache = cache is None
    cache = cache or FootballCache(now=now)
    summary = EnrichmentSummary()

    non_rejected = [c for c in candidates if c.signal_level != "REJECTED"]
    shortlist = sorted(non_rejected, key=lambda c: c.ranking_score, reverse=True)[:ENRICHMENT_SHORTLIST_SIZE]

    if not api_key:
        for c in shortlist:
            c.statistics_source = "unavailable"
            c.final_combined_score = compute_combined_score(c)
        summary.skipped_reason = "FOOTBALL_API_KEY не задан"
        if owns_cache:
            cache.close()
        return summary

    season = _season_for(now)
    summary.season_allowed = season in API_FOOTBALL_FREE_PLAN_SEASONS
    if not summary.season_allowed:
        for c in shortlist:
            c.statistics_source = "unavailable"
            c.final_combined_score = compute_combined_score(c)
        summary.skipped_reason = (
            f"Сезон {season} вне диапазона бесплатного плана API-Football "
            f"({sorted(API_FOOTBALL_FREE_PLAN_SEASONS)}) -- запросы не выполнялись, чтобы не тратить квоту впустую"
        )
        if owns_cache:
            cache.close()
        return summary

    provider = ApiFootballProvider(api_key=api_key, season=season, now=now)

    # Group by real event so both candidates from the same match (if any)
    # share one resolution instead of resolving the same two teams twice.
    by_event: Dict[Tuple[str, str, str], List[ValueCandidate]] = {}
    for c in shortlist:
        by_event.setdefault(_event_key(c), []).append(c)

    requests_used = 0
    for event_key, event_candidates in by_event.items():
        summary.attempted_events += 1
        home_team, away_team = event_key[1], event_key[2]

        if not cache.can_spend(1):
            for c in event_candidates:
                c.statistics_source = "quota_reserved"
                c.final_combined_score = compute_combined_score(c)
            summary.per_event_source[f"{home_team} vs {away_team}"] = "quota_reserved"
            continue

        home_match, home_spent = _resolve_and_cache_team(provider, cache, home_team)
        if home_spent:
            cache.record_requests(home_spent)
            requests_used += home_spent

        if cache.can_spend(1):
            away_match, away_spent = _resolve_and_cache_team(provider, cache, away_team)
        else:
            away_match, away_spent = None, 0
        if away_spent:
            cache.record_requests(away_spent)
            requests_used += away_spent

        if home_match is None or away_match is None or not home_match.matched or not away_match.matched:
            summary.unmatched_events += 1
            for c in event_candidates:
                c.statistics_source = "unmatched"
                c.final_combined_score = compute_combined_score(c)
            summary.per_event_source[f"{home_team} vs {away_team}"] = "unmatched"
            continue

        summary.matched_events += 1

        home_rate, spent_h = (None, 0)
        away_rate, spent_a = (None, 0)
        if cache.can_spend(1):
            home_rate, spent_h = _team_form_win_rate(provider, cache, home_match.team_id, home_match.matched_name)
            if spent_h:
                cache.record_requests(spent_h)
                requests_used += spent_h
        if cache.can_spend(1):
            away_rate, spent_a = _team_form_win_rate(provider, cache, away_match.team_id, away_match.matched_name)
            if spent_a:
                cache.record_requests(spent_a)
                requests_used += spent_a

        retrieved = sum(1 for v in (home_rate, away_rate) if v is not None)
        completeness = retrieved / 2.0

        if home_rate is None and away_rate is None:
            for c in event_candidates:
                c.statistics_source = "unavailable"
                c.statistics_cached = False
                c.statistics_completeness = 0.0
                c.final_combined_score = compute_combined_score(c)
            summary.per_event_source[f"{home_team} vs {away_team}"] = "unavailable"
            continue

        home_edge = (home_rate or 0.0) - (away_rate or 0.0)  # -1..1, positive favors home
        home_edge_0_1 = (home_edge + 1.0) / 2.0

        for c in event_candidates:
            if c.selection == home_match.matched_name or c.selection == home_team:
                agreement = home_edge_0_1
            elif c.selection == away_team or c.selection == away_match.matched_name:
                agreement = 1.0 - home_edge_0_1
            else:
                # A market this simple form signal has no real opinion on
                # (e.g. totals/double chance) -- stay neutral rather than
                # guessing a direction.
                agreement = 0.5
            c.statistics_source = "api_football"
            c.statistics_cached = (spent_h == 0 and spent_a == 0)
            c.statistics_completeness = completeness
            c.statistics_score = round(agreement, 3)
            c.final_combined_score = compute_combined_score(c)
        summary.per_event_source[f"{home_team} vs {away_team}"] = "api_football"

    summary.api_football_requests_used = requests_used
    summary.api_football_quota_remaining_today = cache.requests_available()
    if owns_cache:
        cache.close()
    return summary


def _form_win_rate_and_count(
    provider: ApiFootballProvider, cache: FootballCache, team_name: str,
) -> "tuple[Optional[float], int, int]":
    """Real recent-form win rate (0..1) and real match count for `team_name`,
    using the *exact* API-Football name already known from fixture
    discovery (ai_predictions/fixtures.py) -- no fuzzy search step needed,
    since the fixture itself already identified the real team. Returns
    (win_rate_or_None, matches_counted, requests_spent). Cached by team
    name (24h) so repeated candidates for the same match never re-spend
    quota within a run or across restarts."""
    cache_key = f"form_by_name:{team_name.strip().lower()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached.get("win_rate"), cached.get("matches_counted", 0), 0

    stat = provider.get_home_away_form(team_name)
    if not stat.available:
        # Structural "no data" (free-plan restriction, no finished
        # matches) is worth caching; transient errors are not (handled by
        # the provider's own per-run cache, see api_football.py).
        cache.set(cache_key, {"win_rate": None, "matches_counted": 0})
        return None, 0, 1

    win_rate = _win_rate(stat.value.overall)
    matches_counted = stat.value.matches_counted
    cache.set(cache_key, {"win_rate": win_rate, "matches_counted": matches_counted})
    return win_rate, matches_counted, 1


@dataclass
class FixtureEnrichmentSummary:
    """Summary for the fixture-discovery-first pipeline's enrichment step
    (Phase 5+6): every count here is real, never invented."""
    attempted_events: int = 0
    blended_events: int = 0  # both teams had usable real statistics
    market_only_events: int = 0  # statistics unavailable for at least one side
    api_football_requests_used: int = 0
    api_football_quota_remaining_today: Optional[int] = None
    skipped_reason: Optional[str] = None


def enrich_matched_candidates(
    candidates: List[ValueCandidate],
    fixtures_by_event_id: Dict[str, Fixture],
    *,
    api_key: Optional[str],
    cache: Optional[FootballCache] = None,
    now: Optional[datetime.datetime] = None,
) -> FixtureEnrichmentSummary:
    """Fixture-discovery-first enrichment (Phase 5+6): every candidate here
    already carries a confirmed real API-Football fixture match
    (fixtures_by_event_id[candidate.event_id]), so team resolution is
    exact -- only the real recent-form call itself is needed, saving the
    fuzzy team-search step the older enrich_candidates() still needs.
    Computes the auditable market+statistics probability blend
    (ai_predictions/probability_model.py) and applies it in place via
    value_engine.apply_probability_blend, which can move a candidate
    between HIGH/MEDIUM/LOW/REJECTED. Never fabricates a win rate: a team
    with zero real finished matches retrieved stays statistics_probability
    = None and the candidate stays market-only."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    owns_cache = cache is None
    cache = cache or FootballCache(now=now)
    summary = FixtureEnrichmentSummary()

    non_rejected = [c for c in candidates if c.event_id in fixtures_by_event_id]
    shortlist = sorted(non_rejected, key=lambda c: c.ranking_score, reverse=True)[:ENRICHMENT_SHORTLIST_SIZE]

    def _apply_market_only(c: ValueCandidate) -> None:
        blend = blend_probability(c.consensus_probability, None, 0, 0)
        apply_probability_blend(c, blend)

    if not api_key:
        for c in shortlist:
            _apply_market_only(c)
        summary.skipped_reason = "FOOTBALL_API_KEY не задан"
        if owns_cache:
            cache.close()
        return summary

    provider = ApiFootballProvider(api_key=api_key, now=now)

    by_event: Dict[str, List[ValueCandidate]] = {}
    for c in shortlist:
        by_event.setdefault(c.event_id, []).append(c)

    requests_used = 0
    for event_id, event_candidates in by_event.items():
        summary.attempted_events += 1
        fixture = fixtures_by_event_id[event_id]

        if not cache.can_spend(2):
            for c in event_candidates:
                c.fixture_id = fixture.fixture_id
                c.fixture_match_confidence = getattr(fixture, "match_confidence", None)
                _apply_market_only(c)
            summary.market_only_events += 1
            continue

        home_rate, home_matches, spent_h = _form_win_rate_and_count(provider, cache, fixture.home_team)
        cache.record_requests(spent_h)
        requests_used += spent_h
        away_rate, away_matches, spent_a = _form_win_rate_and_count(provider, cache, fixture.away_team)
        cache.record_requests(spent_a)
        requests_used += spent_a

        for c in event_candidates:
            c.fixture_id = fixture.fixture_id
            if c.selection in (fixture.home_team, "{home}"):
                stats_prob = statistics_probability_for_side(home_rate, away_rate, "home")
            elif c.selection in (fixture.away_team, "{away}"):
                stats_prob = statistics_probability_for_side(home_rate, away_rate, "away")
            else:
                # Draw/totals/double-chance markets -- this simple
                # win-rate signal has no real opinion on them.
                stats_prob = None
            blend = blend_probability(c.consensus_probability, stats_prob, home_matches or 0, away_matches or 0)
            apply_probability_blend(c, blend)

        if any(v is not None for v in (home_rate, away_rate)):
            summary.blended_events += 1
        else:
            summary.market_only_events += 1

    summary.api_football_requests_used = requests_used
    summary.api_football_quota_remaining_today = cache.requests_available()
    if owns_cache:
        cache.close()
    return summary
