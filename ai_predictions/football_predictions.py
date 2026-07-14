"""
Production v3 primary candidate source: builds real, honest market
candidates for one fixture using ONLY API-Football data (predictions
endpoint + recent match history for both teams). The Odds API is never
consulted here -- see ai_predictions/odds_lookup.py for the strictly
optional coefficient enrichment layered on top afterwards.

Every probability produced here traces back to real retrieved numbers:
- 1X2 / double-chance markets use API-Football's own `/predictions`
  percent model when available (it is already a probability estimate,
  not raw odds), or -- only when that endpoint has no percent for this
  fixture -- a fallback derived from each team's real recent home/away
  win rate (ai_predictions/probability_model.py).
- Totals/BTTS markets use the independent-Poisson goal model
  (ai_predictions/goal_model.py) fed with each team's real average goals
  scored/conceded from recent finished matches.

A market is skipped entirely (never a candidate) when the data it needs
could not be retrieved at all -- missing data lowers the *completeness*
of markets that could still be computed from partial data; it never
fabricates the missing piece.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ai_predictions.fixtures import Fixture
from ai_predictions.football_cache import FootballCache
from ai_predictions.goal_model import estimate_total_goals_probabilities
from ai_predictions.probability_model import sample_size_category, statistics_probability_for_side
from ai_predictions.value_config import (
    BET_MARKET_LABELS_RU,
    HISTORICAL_AVG_GOALS_AWAY,
    HISTORICAL_AVG_GOALS_HOME,
    HISTORICAL_AWAY_WIN_PROB,
    HISTORICAL_DRAW_PROB,
    HISTORICAL_FALLBACK_COMPLETENESS,
    HISTORICAL_HOME_WIN_PROB,
    PREDICTIONS_CACHE_TTL_HOURS,
    QUOTA_RESERVE_EXHAUSTED_REASON,
)
from football.interface import Stat
from football.providers.api_football import ApiFootballProvider

#: How many recent finished matches to sample per team -- enough for a
#: real average, small enough to stay cheap (this reuses the provider's
#: own cached-per-run fixture list, so it costs at most one extra network
#: call per team per run, not per market).
RECENT_MATCHES_COUNT = 8

#: Recent-match sample sizes below this count make a market's inputs
#: "incomplete" even though a number could technically be computed --
#: keeps completeness honest rather than treating 1-2 matches like 8.
FULL_SAMPLE_MATCHES = RECENT_MATCHES_COUNT


@dataclass
class MarketCandidate:
    fixture: Fixture
    market_key: str
    market_label_ru: str
    probability: float  # 0..1, honest, derived from real retrieved data
    completeness: float  # 0..1
    sample_size_category: str  # none|weak|medium|strong
    rationale: str
    source: str  # "api_football_predictions" | "recent_form" | "goal_model"


@dataclass
class TeamRecentStats:
    matches_counted: int = 0
    win_rate: Optional[float] = None
    avg_scored: Optional[float] = None
    avg_conceded: Optional[float] = None
    available: bool = False
    reason: Optional[str] = None


def _spend_and_call(provider: ApiFootballProvider, cache: FootballCache, fn, *args, **kwargs) -> Stat:
    """Only spends real API-Football requests while today's safety
    reserve allows it. A cache MISS that also has no budget left returns
    Stat.missing(QUOTA_RESERVE_EXHAUSTED_REASON) without touching the
    network -- callers must treat this exactly like any other missing
    data (fall back to a lower-confidence signal), never abort the whole
    fixture/run."""
    if not cache.can_spend(1):
        return Stat.missing(QUOTA_RESERVE_EXHAUSTED_REASON)
    before = provider.requests_made
    result = fn(*args, **kwargs)
    spent = provider.requests_made - before
    if spent:
        cache.record_requests(spent)
    return result


def _team_recent_stats(provider: ApiFootballProvider, cache: FootballCache, team: str) -> TeamRecentStats:
    """One real, 24h-cached bundle per team: win rate + average
    scored/conceded goals over `RECENT_MATCHES_COUNT` real finished
    matches. Cached persistently so a team appearing in several of this
    run's fixtures (or a later run within 24h) never re-triggers the
    network call. A cache MISS is only ever escalated to a real network
    call while today's quota reserve allows it -- once exhausted, this
    returns "unavailable" (never blocking the fixture, only this one
    ingredient of it)."""
    cache_key = f"team_recent_stats:{team.strip().lower()}:{RECENT_MATCHES_COUNT}"
    cached = cache.get(cache_key)
    if cached is not None:
        return TeamRecentStats(**cached)

    stat = _spend_and_call(provider, cache, provider.get_last_matches, team, count=RECENT_MATCHES_COUNT)
    if not stat.available:
        # Transient errors (including quota-reserve exhaustion) are not
        # cached here either -- same rule as the provider's own per-run
        # caches (never cache a "not found yet").
        return TeamRecentStats(available=False, reason=stat.reason)

    matches = stat.value
    wins = 0
    scored_values: List[int] = []
    conceded_values: List[int] = []
    for m in matches:
        is_home = m.home_team == team
        scored = m.home_goals if is_home else m.away_goals
        conceded = m.away_goals if is_home else m.home_goals
        if scored is None or conceded is None:
            continue
        scored_values.append(scored)
        conceded_values.append(conceded)
        if scored > conceded:
            wins += 1

    if not scored_values:
        result = TeamRecentStats(available=False, reason=f"Нет данных о голах для «{team}»")
        return result

    result = TeamRecentStats(
        matches_counted=len(scored_values),
        win_rate=wins / len(scored_values),
        avg_scored=sum(scored_values) / len(scored_values),
        avg_conceded=sum(conceded_values) / len(conceded_values),
        available=True,
    )
    cache.set(cache_key, {
        "matches_counted": result.matches_counted,
        "win_rate": result.win_rate,
        "avg_scored": result.avg_scored,
        "avg_conceded": result.avg_conceded,
        "available": True,
        "reason": None,
    })
    return result


def _parse_percent(raw: Optional[str]) -> Optional[float]:
    if not raw:
        return None
    try:
        return float(str(raw).replace("%", "").strip()) / 100.0
    except ValueError:
        return None


def _predictions_for_fixture(fixture: Fixture, provider: ApiFootballProvider, cache: FootballCache) -> Stat:
    """Real `/predictions` answer for this fixture, persistently cached
    for PREDICTIONS_CACHE_TTL_HOURS so repeated runs on the same day never
    re-spend quota on a fixture already analysed. A cache MISS only
    escalates to a real network call while today's quota reserve allows
    it (see _spend_and_call) -- once exhausted, this fixture simply
    proceeds without a predictions-endpoint opinion, never blocking it."""
    cache_key = f"predictions:{fixture.fixture_id}"
    cached = cache.get(cache_key, ttl_hours=PREDICTIONS_CACHE_TTL_HOURS)
    if cached is not None:
        if cached.get("available"):
            return Stat.ok(cached["data"])
        return Stat.missing(cached.get("reason") or "Прогноз недоступен (по данным кэша)")

    stat = _spend_and_call(provider, cache, provider.get_predictions, fixture.fixture_id)
    if stat.reason == QUOTA_RESERVE_EXHAUSTED_REASON:
        # Transient (no budget left this run) -- do not persist, a later
        # run today (or tomorrow) may still get a real answer.
        return stat
    if stat.available:
        cache.set(cache_key, {"available": True, "data": stat.value})
    else:
        # A confirmed real "no predictions for this fixture" answer (the
        # provider only reaches this branch after a successful HTTP call
        # -- see ApiFootballProvider.get_predictions) is worth caching so
        # we don't keep re-asking API-Football the same question.
        cache.set(cache_key, {"available": False, "reason": stat.reason})
    return stat


def _historical_baseline_candidates(fixture: Fixture) -> List[MarketCandidate]:
    """Last-resort fallback when NEITHER the predictions endpoint NOR
    either team's recent form could be retrieved (no cache hit and no
    quota left). Uses real, well-documented aggregate football statistics
    (see value_config.HISTORICAL_*) -- never fabricated for this specific
    match -- so the fixture is still ranked instead of silently dropped.
    Always carries sample_size_category="none", which caps it at the LOW
    confidence tier regardless of the raw probability (see
    prediction_selector.classify)."""
    candidates: List[MarketCandidate] = []
    rationale = (
        "Статистика по этому матчу недоступна (исчерпан дневной резерв запросов "
        "к API-Football и данные ещё не кэшированы) — используется обобщённая "
        "историческая статистика, уверенность снижена."
    )
    entries = [
        ("home_win", HISTORICAL_HOME_WIN_PROB),
        ("draw", HISTORICAL_DRAW_PROB),
        ("away_win", HISTORICAL_AWAY_WIN_PROB),
        ("double_chance_1x", HISTORICAL_HOME_WIN_PROB + HISTORICAL_DRAW_PROB),
        ("double_chance_x2", HISTORICAL_AWAY_WIN_PROB + HISTORICAL_DRAW_PROB),
    ]
    for market_key, probability in entries:
        candidates.append(MarketCandidate(
            fixture=fixture,
            market_key=market_key,
            market_label_ru=BET_MARKET_LABELS_RU[market_key],
            probability=max(0.0, min(1.0, probability)),
            completeness=HISTORICAL_FALLBACK_COMPLETENESS,
            sample_size_category="none",
            rationale=rationale,
            source="historical_baseline",
        ))

    goals = estimate_total_goals_probabilities(HISTORICAL_AVG_GOALS_HOME, HISTORICAL_AVG_GOALS_AWAY)
    for market_key, probability in (
        ("over_1_5", goals.over_1_5), ("over_2_5", goals.over_2_5), ("under_3_5", goals.under_3_5),
        ("btts_yes", goals.btts_yes), ("btts_no", goals.btts_no),
    ):
        candidates.append(MarketCandidate(
            fixture=fixture,
            market_key=market_key,
            market_label_ru=BET_MARKET_LABELS_RU[market_key],
            probability=max(0.0, min(1.0, probability)),
            completeness=HISTORICAL_FALLBACK_COMPLETENESS,
            sample_size_category="none",
            rationale=rationale,
            source="historical_baseline",
        ))
    return candidates


def _completeness_for(*, has_predictions_percent: bool, home_stats: TeamRecentStats, away_stats: TeamRecentStats) -> float:
    """0..1 -- how much of this market's evidence is real, retrieved data
    at (near-)full sample size. A market missing recent-form entirely
    (predictions-percent-only) still gets a moderate completeness score,
    never a fabricated 1.0."""
    score = 0.0
    weight_total = 0.0

    weight_total += 1.0
    if has_predictions_percent:
        score += 1.0

    for stats in (home_stats, away_stats):
        weight_total += 1.0
        if stats.available:
            score += min(1.0, stats.matches_counted / FULL_SAMPLE_MATCHES)

    return score / weight_total if weight_total else 0.0


def build_candidates_for_fixture(
    fixture: Fixture,
    provider: ApiFootballProvider,
    cache: FootballCache,
) -> "tuple[List[MarketCandidate], int]":
    """Returns (candidates, requests_used_beyond_provider_internal_cache).
    `requests_used` is only a diagnostic estimate for /status -- the real
    accounting is the provider's own `requests_made` counter, which the
    caller (football_pipeline.py) reads directly."""
    candidates: List[MarketCandidate] = []

    predictions_stat = _predictions_for_fixture(fixture, provider, cache)
    percent_home = percent_draw = percent_away = None
    predictions_available = False
    if predictions_stat.available:
        percent = predictions_stat.value.get("percent") or {}
        percent_home = _parse_percent(percent.get("home"))
        percent_draw = _parse_percent(percent.get("draw"))
        percent_away = _parse_percent(percent.get("away"))
        predictions_available = percent_home is not None and percent_draw is not None and percent_away is not None

    home_stats = _team_recent_stats(provider, cache, fixture.home_team)
    away_stats = _team_recent_stats(provider, cache, fixture.away_team)

    # -- 1X2 + double chance -------------------------------------------------
    if not predictions_available:
        # Fallback: real recent home/away win rates, when available.
        home_rate = home_stats.win_rate if home_stats.available else None
        away_rate = away_stats.win_rate if away_stats.available else None
        implied_home = statistics_probability_for_side(home_rate, away_rate, "home")
        implied_away = statistics_probability_for_side(home_rate, away_rate, "away")
        if implied_home is not None and implied_away is not None:
            percent_home, percent_away = implied_home, implied_away
            percent_draw = max(0.0, 1.0 - percent_home - percent_away)

    if percent_home is not None and percent_draw is not None and percent_away is not None:
        completeness = _completeness_for(
            has_predictions_percent=predictions_available, home_stats=home_stats, away_stats=away_stats,
        )
        size_category = sample_size_category(home_stats.matches_counted, away_stats.matches_counted)
        source = "api_football_predictions" if predictions_available else "recent_form"
        rationale_source = (
            "по собственной модели API-Football" if predictions_available
            else f"по реальной статистике последних матчей ({fixture.home_team}: {home_stats.matches_counted} игр, "
                 f"{fixture.away_team}: {away_stats.matches_counted} игр)"
        )
        entries = [
            ("home_win", percent_home),
            ("draw", percent_draw),
            ("away_win", percent_away),
            ("double_chance_1x", percent_home + percent_draw),
            ("double_chance_x2", percent_away + percent_draw),
        ]
        for market_key, probability in entries:
            candidates.append(MarketCandidate(
                fixture=fixture,
                market_key=market_key,
                market_label_ru=BET_MARKET_LABELS_RU[market_key],
                probability=max(0.0, min(1.0, probability)),
                completeness=completeness,
                sample_size_category=size_category,
                rationale=f"Расчётная вероятность {rationale_source}.",
                source=source,
            ))

    # -- totals / BTTS (independent-Poisson goal model) ----------------------
    if home_stats.available and away_stats.available:
        expected_home_goals = (home_stats.avg_scored + away_stats.avg_conceded) / 2.0
        expected_away_goals = (away_stats.avg_scored + home_stats.avg_conceded) / 2.0
        goals = estimate_total_goals_probabilities(expected_home_goals, expected_away_goals)
        completeness = _completeness_for(
            has_predictions_percent=False, home_stats=home_stats, away_stats=away_stats,
        )
        size_category = sample_size_category(home_stats.matches_counted, away_stats.matches_counted)
        rationale = (
            f"Модель по среднему количеству голов последних матчей: {fixture.home_team} — "
            f"{home_stats.avg_scored:.1f} забито/{home_stats.avg_conceded:.1f} пропущено, "
            f"{fixture.away_team} — {away_stats.avg_scored:.1f} забито/{away_stats.avg_conceded:.1f} пропущено "
            f"(последние {home_stats.matches_counted} и {away_stats.matches_counted} матчей)."
        )
        for market_key, probability in (
            ("over_1_5", goals.over_1_5), ("over_2_5", goals.over_2_5), ("under_3_5", goals.under_3_5),
            ("btts_yes", goals.btts_yes), ("btts_no", goals.btts_no),
        ):
            candidates.append(MarketCandidate(
                fixture=fixture,
                market_key=market_key,
                market_label_ru=BET_MARKET_LABELS_RU[market_key],
                probability=max(0.0, min(1.0, probability)),
                completeness=completeness,
                sample_size_category=size_category,
                rationale=rationale,
                source="goal_model",
            ))

    # -- historical-baseline fallback -----------------------------------------
    # Reached only when NOTHING real was retrieved for this fixture at all
    # (no predictions endpoint answer, no team-derived 1X2 estimate, and no
    # totals/BTTS model) -- e.g. quota reserve exhausted and neither team
    # has any cached data yet. Per the "never return zero analysis when
    # fixtures exist" requirement, the fixture is still ranked using real,
    # generic historical statistics instead of being skipped, always capped
    # at LOW confidence (sample_size_category="none").
    if not candidates:
        candidates.extend(_historical_baseline_candidates(fixture))

    return candidates, provider.requests_made
