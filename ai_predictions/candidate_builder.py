"""
Builds real CandidatePrediction objects for one football event by combining:
  1. cross-bookmaker consensus probability from the raw Odds API event
     (margin removed via selection_engine.scoring.bookmaker_probability),
  2. statistics-based probability from football/prediction.py's
     deterministic confidence engine (via ApiFootballProvider), scaled from
     its 0..100 confidence to a 0..1 probability proxy.

Never invents a market: a candidate is only produced for a market family
actually present in the Odds API response for that event. Markets with no
clean, unambiguous shape to parse safely (Asian handicap "spreads", and
per-team "team_totals", whose outcome shape varies by provider) are
intentionally not mapped in this first version -- see module docstring in
ai_predictions/__init__.py.

If neither a statistics-based nor a consensus-odds probability could be
computed for a given outcome, no candidate is produced for it at all
(there is nothing real to build one from).
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Tuple

from football.interface import FootballStatisticsProvider
from football.prediction import MarketResult, analyze_match
from selection_engine.config import (
    MARKET_1X2,
    MARKET_BTTS,
    MARKET_DOUBLE_CHANCE,
    MARKET_DRAW_NO_BET,
    MARKET_TOTAL_GOALS,
)
from selection_engine.models import CandidatePrediction
from selection_engine.scoring import bookmaker_probability

# Statistics-based probability and cross-bookmaker consensus are blended in
# equal parts when both are available -- neither has been backtested yet
# to deserve more trust than the other; see replit.md / task report for
# how this default could be revisited once calibration data accumulates.
STATS_WEIGHT = 0.5
CONSENSUS_WEIGHT = 0.5

TOTAL_GOALS_OVER_THRESHOLDS = (0.5, 1.5, 2.5, 3.5)
TOTAL_GOALS_UNDER_THRESHOLDS = (2.5,)


def _blend_probability(stats_prob: Optional[float], consensus_prob: Optional[float]) -> Optional[float]:
    if stats_prob is None and consensus_prob is None:
        return None
    if stats_prob is None:
        return consensus_prob
    if consensus_prob is None:
        return stats_prob
    return STATS_WEIGHT * stats_prob + CONSENSUS_WEIGHT * consensus_prob


def _stats_probability(market_result: Optional[MarketResult]) -> Optional[float]:
    if market_result is None or market_result.status == "unavailable":
        return None
    return max(0.0, min(1.0, market_result.confidence / 100.0))


def _by_family(results: List[MarketResult]) -> Dict[str, MarketResult]:
    return {r.family: r for r in results}


def _find_total_goals_result(results: List[MarketResult], *, over: bool, threshold: float) -> Optional[MarketResult]:
    target = f"Тотал больше {threshold}" if over else f"Тотал меньше {threshold}"
    for r in results:
        if r.market_name == target:
            return r
    return None


# ---------------------------------------------------------------------------
# Odds consensus
# ---------------------------------------------------------------------------

def _bookmaker_entries(event: Dict[str, Any], market_key: str) -> List[Tuple[str, str, List[Dict[str, Any]]]]:
    """Returns (bookmaker_title, last_update, outcomes) for every bookmaker
    offering `market_key` on this event."""
    entries = []
    for bookmaker in event.get("bookmakers", []) or []:
        for market in bookmaker.get("markets", []) or []:
            if market.get("key") == market_key:
                entries.append((
                    bookmaker.get("title", "?"),
                    bookmaker.get("last_update", ""),
                    market.get("outcomes", []) or [],
                ))
    return entries


def _consensus_for_selection(
    event: Dict[str, Any],
    market_key: str,
    outcome_matcher,
    *,
    point: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """`outcome_matcher(outcome_name) -> bool` identifies the target
    selection within a bookmaker's full outcome list for this market. When
    `point` is given, only bookmaker entries whose outcomes carry that exact
    point (within a small tolerance) are considered -- so Over 2.5 is never
    averaged together with Over 3.0 from a different bookmaker.

    Returns None if no bookmaker offered this exact selection/line, else a
    dict with probability (margin-adjusted average), best_price,
    best_bookmaker, bookmaker_count, latest_update."""
    probs: List[float] = []
    best_price: Optional[float] = None
    best_bookmaker: Optional[str] = None
    latest_update = ""

    for title, last_update, outcomes in _bookmaker_entries(event, market_key):
        if point is not None:
            outcomes = [o for o in outcomes if o.get("point") is not None and abs(float(o["point"]) - point) < 0.01]
            if not outcomes:
                continue
            # Complete market for margin removal = every outcome at this point.
            complete = outcomes
        else:
            complete = outcomes

        target_outcome = next((o for o in outcomes if outcome_matcher(o.get("name", ""))), None)
        if target_outcome is None:
            continue
        try:
            price = float(target_outcome.get("price"))
            complete_prices = [float(o.get("price")) for o in complete if o.get("price") is not None]
        except (TypeError, ValueError):
            continue
        if price <= 1.0 or len(complete_prices) < 2:
            continue

        _, adjusted, is_adjusted = bookmaker_probability(price, complete_prices)
        probs.append(adjusted if is_adjusted else (1.0 / price))

        if best_price is None or price > best_price:
            best_price = price
            best_bookmaker = title
        if last_update and last_update > latest_update:
            latest_update = last_update

    if not probs or best_price is None:
        return None

    return {
        "probability": sum(probs) / len(probs),
        "best_price": best_price,
        "best_bookmaker": best_bookmaker,
        "bookmaker_count": len(probs),
        "latest_update": latest_update or None,
    }


# ---------------------------------------------------------------------------
# Candidate assembly
# ---------------------------------------------------------------------------

def _base_kwargs(
    event: Dict[str, Any],
    event_id: str,
    league: Optional[str],
) -> Dict[str, Any]:
    return dict(
        event_id=event_id,
        sport="football",
        league=league,
        country=None,
        match_datetime=event.get("commence_time", ""),
        home_team=event.get("home_team", ""),
        away_team=event.get("away_team", ""),
    )


def _sample_size(ctx) -> int:
    home_n = ctx.home_last.value if ctx.home_last.available else None
    away_n = ctx.away_last.value if ctx.away_last.available else None
    if not home_n or not away_n:
        return 0
    return min(len(home_n), len(away_n))


def _explanation_and_risks(
    stats_result: Optional[MarketResult],
    consensus: Optional[Dict[str, Any]],
) -> Tuple[List[str], List[str]]:
    explanation: List[str] = []
    risks: List[str] = []
    if stats_result is not None and stats_result.status != "unavailable":
        explanation.extend(stats_result.explanation)
        risks.extend(stats_result.missing_statistics)
        if stats_result.status == "unavailable":
            risks.append("Статистическая модель не дала оценки по этому рынку")
    else:
        risks.append("Нет статистической оценки для этого рынка — используется только консенсус котировок")
    if consensus is not None:
        explanation.append(f"Консенсус {consensus['bookmaker_count']} букмекер(ов) учтён при оценке вероятности")
    else:
        risks.append("Нет рыночных котировок для консенсуса — используется только статистическая модель")
    return explanation, risks


def _make_candidate(
    *,
    event: Dict[str, Any],
    event_id: str,
    league: Optional[str],
    market_type: str,
    selection: str,
    line: Optional[float],
    consensus: Optional[Dict[str, Any]],
    stats_result: Optional[MarketResult],
    sample_size: int,
    available_fields: Dict[str, bool],
) -> Optional[CandidatePrediction]:
    stats_prob = _stats_probability(stats_result)
    consensus_prob = consensus["probability"] if consensus else None
    model_probability = _blend_probability(stats_prob, consensus_prob)
    if model_probability is None or consensus is None:
        # No real market price to attach a candidate to -- an odds-less
        # "prediction" cannot be evaluated for edge/EV and is not produced.
        return None

    explanation, risks = _explanation_and_risks(stats_result, consensus)
    is_contradictory = bool(stats_result and stats_result.status != "unavailable" and
                             "расходятся" in " ".join(stats_result.explanation))

    return CandidatePrediction(
        **_base_kwargs(event, event_id, league),
        market_type=market_type,
        selection=selection,
        line=line,
        bookmaker=consensus["best_bookmaker"] or "?",
        odds=consensus["best_price"],
        model_probability=model_probability,
        sample_size=sample_size,
        available_fields=available_fields,
        explanation=explanation,
        risk_factors=risks,
        is_contradictory=is_contradictory,
        price_timestamp=consensus["latest_update"],
        source_data_timestamp=consensus["latest_update"],
    )


def build_candidates_for_event(
    event: Dict[str, Any],
    provider: FootballStatisticsProvider,
    *,
    event_id: str,
    league: Optional[str] = None,
    stats_count: int = 10,
) -> List[CandidatePrediction]:
    """Builds every real candidate this event's Odds API data supports. May
    return an empty list if the event has no usable markets or statistics
    -- this function never pads output."""
    home_team = event.get("home_team", "")
    away_team = event.get("away_team", "")
    if not home_team or not away_team:
        return []

    ctx = None
    market_results: List[MarketResult] = []
    try:
        ctx, market_results = analyze_match(provider, home_team, away_team, league, stats_count)
    except Exception:
        # Statistics are optional per-candidate (consensus-only fallback);
        # a provider failure must never crash the whole pipeline.
        ctx = None
        market_results = []

    family_map = _by_family(market_results) if market_results else {}
    n = _sample_size(ctx) if ctx else 0

    common_fields_1x2 = {
        "home_form": bool(ctx and ctx.home_form.available),
        "away_form": bool(ctx and ctx.away_form.available),
        "sample_size": n > 0,
        "h2h": bool(ctx and ctx.h2h.available),
        "league_position": bool(ctx and ctx.standings.available),
        "injuries": bool(ctx and ctx.home_injuries.available and ctx.away_injuries.available),
        "lineups": bool(ctx and ctx.lineups.available),
    }

    candidates: List[CandidatePrediction] = []

    # -- 1x2 -----------------------------------------------------------------
    for selection_name, family, matcher in (
        (home_team, "home_win", lambda name: name == home_team),
        ("Draw", "draw", lambda name: name.lower() == "draw"),
        (away_team, "away_win", lambda name: name == away_team),
    ):
        consensus = _consensus_for_selection(event, "h2h", matcher)
        candidate = _make_candidate(
            event=event, event_id=event_id, league=league,
            market_type=MARKET_1X2, selection=selection_name, line=None,
            consensus=consensus, stats_result=family_map.get(family),
            sample_size=n, available_fields=dict(common_fields_1x2),
        )
        if candidate:
            candidates.append(candidate)

    # -- double chance ---------------------------------------------------------
    for selection_name, family, matcher in (
        (f"{home_team} или ничья", "double_chance_1x", lambda name: _matches_double_chance(name, home_team, None)),
        (f"ничья или {away_team}", "double_chance_x2", lambda name: _matches_double_chance(name, None, away_team)),
        (f"{home_team} или {away_team}", "double_chance_12", lambda name: _matches_double_chance(name, home_team, away_team)),
    ):
        consensus = _consensus_for_selection(event, "double_chance", matcher)
        candidate = _make_candidate(
            event=event, event_id=event_id, league=league,
            market_type=MARKET_DOUBLE_CHANCE, selection=selection_name, line=None,
            consensus=consensus, stats_result=family_map.get(family),
            sample_size=n, available_fields=dict(common_fields_1x2),
        )
        if candidate:
            candidates.append(candidate)

    # -- draw no bet -------------------------------------------------------
    home_win_result = family_map.get("home_win")
    away_win_result = family_map.get("away_win")
    dnb_home_stats = _normalized_draw_no_bet(home_win_result, away_win_result, for_home=True)
    dnb_away_stats = _normalized_draw_no_bet(home_win_result, away_win_result, for_home=False)
    for selection_name, synthetic_conf, matcher in (
        (home_team, dnb_home_stats, lambda name: name == home_team),
        (away_team, dnb_away_stats, lambda name: name == away_team),
    ):
        consensus = _consensus_for_selection(event, "draw_no_bet", matcher)
        synthetic_result = _synthetic_market_result(synthetic_conf)
        candidate = _make_candidate(
            event=event, event_id=event_id, league=league,
            market_type=MARKET_DRAW_NO_BET, selection=selection_name, line=None,
            consensus=consensus, stats_result=synthetic_result,
            sample_size=n, available_fields=dict(common_fields_1x2),
        )
        if candidate:
            candidates.append(candidate)

    # -- BTTS -----------------------------------------------------------------
    btts_fields = {
        "btts_frequency_home": bool(ctx and ctx.home_btts.available),
        "btts_frequency_away": bool(ctx and ctx.away_btts.available),
        "sample_size": n > 0,
        "clean_sheets_home": bool(ctx and ctx.home_clean.available),
        "clean_sheets_away": bool(ctx and ctx.away_clean.available),
        "goals_scored_conceded": bool(ctx and ctx.home_last.available and ctx.away_last.available),
    }
    for selection_name, family, matcher in (
        ("Да", "btts_yes", lambda name: name.lower() in ("yes", "да")),
        ("Нет", "btts_no", lambda name: name.lower() in ("no", "нет")),
    ):
        consensus = _consensus_for_selection(event, "btts", matcher)
        candidate = _make_candidate(
            event=event, event_id=event_id, league=league,
            market_type=MARKET_BTTS, selection=selection_name, line=None,
            consensus=consensus, stats_result=family_map.get(family),
            sample_size=n, available_fields=dict(btts_fields),
        )
        if candidate:
            candidates.append(candidate)

    # -- total goals -----------------------------------------------------------
    total_fields = {
        "goals_scored_conceded": bool(ctx and ctx.home_last.available and ctx.away_last.available),
        "sample_size": n > 0,
        "current_price": True,  # only reached when a real price was found
        "h2h": bool(ctx and ctx.h2h.available),
        "league_position": bool(ctx and ctx.standings.available),
    }
    for point in _distinct_totals_points(event):
        over_stats = _find_total_goals_result(market_results, over=True, threshold=point) if point in TOTAL_GOALS_OVER_THRESHOLDS else None
        under_stats = _find_total_goals_result(market_results, over=False, threshold=point) if point in TOTAL_GOALS_UNDER_THRESHOLDS else None
        for selection_name, stats_result, matcher in (
            ("Over", over_stats, lambda name: name.lower() == "over"),
            ("Under", under_stats, lambda name: name.lower() == "under"),
        ):
            consensus = _consensus_for_selection(event, "totals", matcher, point=point)
            candidate = _make_candidate(
                event=event, event_id=event_id, league=league,
                market_type=MARKET_TOTAL_GOALS, selection=f"{selection_name} {point}", line=point,
                consensus=consensus, stats_result=stats_result,
                sample_size=n, available_fields=dict(total_fields),
            )
            if candidate:
                candidates.append(candidate)

    return candidates


def _distinct_totals_points(event: Dict[str, Any]) -> List[float]:
    points = set()
    for _, _, outcomes in _bookmaker_entries(event, "totals"):
        for o in outcomes:
            if o.get("point") is not None:
                try:
                    points.add(round(float(o["point"]), 2))
                except (TypeError, ValueError):
                    continue
    return sorted(points)


def _matches_double_chance(name: str, home_team: Optional[str], away_team: Optional[str]) -> bool:
    lowered = name.lower()
    has_home = home_team is not None and home_team.lower() in lowered
    has_away = away_team is not None and away_team.lower() in lowered
    has_draw = "draw" in lowered or "x" == lowered.strip()
    if home_team and away_team:
        return has_home and has_away
    if home_team:
        return has_home and (has_draw or "1x" in lowered.replace(" ", ""))
    if away_team:
        return has_away and (has_draw or "x2" in lowered.replace(" ", ""))
    return False


def _normalized_draw_no_bet(
    home_win_result: Optional[MarketResult],
    away_win_result: Optional[MarketResult],
    *,
    for_home: bool,
) -> Optional[float]:
    """Draw-no-bet probability estimate: the two outright win confidences,
    renormalised over just those two outcomes (draws are excluded from the
    market by definition, so their combined confidence -- not draw's -- is
    the right thing to redistribute). Returns None (no invented number) if
    either outright result is unavailable."""
    home_conf = _stats_probability(home_win_result)
    away_conf = _stats_probability(away_win_result)
    if home_conf is None or away_conf is None:
        return None
    total = home_conf + away_conf
    if total <= 0:
        return None
    return (home_conf / total) if for_home else (away_conf / total)


def _synthetic_market_result(probability: Optional[float]) -> Optional[MarketResult]:
    """Wraps a derived (not directly provider-sourced) probability estimate
    in a MarketResult shape so it can flow through the same
    `_stats_probability`/explanation helpers as a directly-computed one."""
    if probability is None:
        return None
    return MarketResult(
        market_name="Без ничьей (расчётно)",
        market_type="match_result",
        confidence=probability * 100.0,
        strength="", risk="", stars=0,
        explanation=["вероятность рассчитана из побед хозяев/гостей без учёта ничьей"],
        supporting_statistics=[], missing_statistics=[],
        status="secondary",
        family="draw_no_bet_derived",
    )
