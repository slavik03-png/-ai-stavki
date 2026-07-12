"""
Cross-bookmaker price-divergence value detection.

Independent of any football statistics provider: every number produced
here comes directly from real bookmaker prices already fetched from The
Odds API for a single event. No team-strength model, no API-Football
call, and no invented number is involved anywhere in this module.

Core idea (classic "beat the market" value betting, not a statistical
model): for a given real selection (e.g. "Arsenal to win"), take every
bookmaker quoting it, remove each bookmaker's own margin from their full
outcome set, and average the *other* bookmakers' margin-free probabilities
(leave-one-out) to build a real, independent "consensus fair probability".
Compare that to the margin-free probability implied by the single best
price on offer. When the best price is priced notably higher than what
the rest of the market thinks is fair, that gap ("edge") is real, verified
divergence between real bookmakers -- not a guess about the match itself.

This deliberately does not reuse selection_engine's CandidatePrediction /
SelectionConfig machinery: that engine's confidence scoring, sample
reliability and historical-performance blending are built around having a
separate statistics-based model_probability, which does not exist here.
Reusing it would risk quietly presenting a "confidence score" that implies
statistical modeling that never happened.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ai_predictions.candidate_builder import (
    _bookmaker_entries,
    _distinct_totals_points,
    _matches_double_chance,
)
from selection_engine.config import (
    MARKET_1X2,
    MARKET_DOUBLE_CHANCE,
    MARKET_DRAW_NO_BET,
    MARKET_TOTAL_GOALS,
)
from selection_engine.scoring import normalise_market_probabilities, raw_implied_probability

#: The Odds API market key -> our internal market_type (kept identical to
#: tracking.settlement's market types so every saved prediction can later
#: be graded). "spreads" (Asian handicap) is intentionally excluded: the
#: tracking/settlement package has no handicap-settlement function today,
#: so a spreads recommendation could never be settled -- consistent with
#: this codebase's rule of never producing a candidate for a market the
#: tracking system cannot later grade.
MARKET_KEY_TO_TYPE: Dict[str, str] = {
    "h2h": MARKET_1X2,
    "double_chance": MARKET_DOUBLE_CHANCE,
    "draw_no_bet": MARKET_DRAW_NO_BET,
    "totals": MARKET_TOTAL_GOALS,
}

#: A recommendation needs at least this many independent real bookmaker
#: quotes for the exact same selection/line to be considered reliable
#: enough to call "consensus" at all.
MIN_BOOKMAKERS = 3

#: Minimum real leave-one-out edge (consensus probability minus the best
#: price's own implied probability) to call the divergence genuine value
#: rather than ordinary bookmaker-to-bookmaker pricing noise. Same order
#: of magnitude as selection_engine's own min_edge default (0.04) --
#: documented business threshold, not derived from any invented figure.
MIN_EDGE = 0.03

MAX_MAIN_RECOMMENDATIONS = 5


@dataclass
class ValueCandidate:
    event_id: str
    sport: str
    league: Optional[str]
    country: Optional[str]
    match_datetime: str
    home_team: str
    away_team: str
    market_type: str
    selection: str
    line: Optional[float]

    best_bookmaker: str
    best_price: float
    best_price_implied_probability: float  # margin-removed, from the best bookmaker's own full market

    consensus_probability: float  # leave-one-out, margin-removed, average of every OTHER bookmaker
    consensus_bookmaker_count: int  # how many "other" bookmakers fed the consensus
    fair_price: float  # 1 / consensus_probability

    edge: float  # consensus_probability - best_price_implied_probability
    expected_value: float  # consensus_probability * best_price - 1

    bookmaker_count: int  # total bookmakers quoting this exact selection/line (including the best one)
    all_prices: List[float] = field(default_factory=list)

    rejection_reasons: List[str] = field(default_factory=list)
    generated_at: str = ""

    def __post_init__(self) -> None:
        if not self.generated_at:
            self.generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    @property
    def passed(self) -> bool:
        return not self.rejection_reasons


def _margin_free_probability(price: float, complete_prices: List[float]) -> Optional[float]:
    if price <= 1.0 or len(complete_prices) < 2:
        return None
    try:
        raw_all = [raw_implied_probability(p) for p in complete_prices]
    except ValueError:
        return None
    normalised = normalise_market_probabilities(raw_all)
    raw = raw_implied_probability(price)
    return raw / sum(raw_all)


def _selection_matchers(market_key: str, home_team: str, away_team: str):
    """Returns (selection_label, outcome_matcher) pairs for markets that
    have no line/point (h2h, double_chance, draw_no_bet)."""
    if market_key == "h2h":
        return [
            (home_team, lambda name: name == home_team),
            ("Ничья", lambda name: name.lower() in ("draw", "ничья")),
            (away_team, lambda name: name == away_team),
        ]
    if market_key == "double_chance":
        return [
            (f"{home_team} или ничья", lambda name: _matches_double_chance(name, home_team, None)),
            (f"ничья или {away_team}", lambda name: _matches_double_chance(name, None, away_team)),
            (f"{home_team} или {away_team}", lambda name: _matches_double_chance(name, home_team, away_team)),
        ]
    if market_key == "draw_no_bet":
        return [
            (home_team, lambda name: name == home_team),
            (away_team, lambda name: name == away_team),
        ]
    return []


def _build_for_selection(
    *,
    event: Dict[str, Any],
    event_id: str,
    league: Optional[str],
    market_key: str,
    market_type: str,
    selection_name: str,
    outcome_matcher,
    home_team: str,
    away_team: str,
    match_datetime: str,
    point: Optional[float] = None,
) -> Optional[ValueCandidate]:
    """Gathers every bookmaker's real price for one selection/line, computes
    the leave-one-out consensus and the best-price edge, and returns a
    ValueCandidate (with rejection_reasons populated if it does not meet
    the minimum bookmaker count) or None if no bookmaker offered this
    selection/line at all (nothing real to build a candidate from)."""
    quotes: List[Dict[str, Any]] = []  # {"bookmaker", "price", "complete_prices"}

    for title, _last_update, outcomes in _bookmaker_entries(event, market_key):
        if point is not None:
            outcomes = [o for o in outcomes if o.get("point") is not None and abs(float(o["point"]) - point) < 0.01]
            if not outcomes:
                continue
        target = next((o for o in outcomes if outcome_matcher(o.get("name", ""))), None)
        if target is None:
            continue
        try:
            price = float(target.get("price"))
            complete_prices = [float(o.get("price")) for o in outcomes if o.get("price") is not None]
        except (TypeError, ValueError):
            continue
        if price <= 1.0 or len(complete_prices) < 2:
            continue
        quotes.append({"bookmaker": title, "price": price, "complete_prices": complete_prices})

    if not quotes:
        return None

    best = max(quotes, key=lambda q: q["price"])
    best_prob = _margin_free_probability(best["price"], best["complete_prices"])
    if best_prob is None:
        return None

    others = [q for q in quotes if q is not best]
    other_probs = []
    for q in others:
        p = _margin_free_probability(q["price"], q["complete_prices"])
        if p is not None:
            other_probs.append(p)

    candidate = ValueCandidate(
        event_id=event_id,
        sport="soccer",
        league=league,
        country=None,
        match_datetime=match_datetime,
        home_team=home_team,
        away_team=away_team,
        market_type=market_type,
        selection=selection_name,
        line=point,
        best_bookmaker=best["bookmaker"],
        best_price=best["price"],
        best_price_implied_probability=best_prob,
        consensus_probability=(sum(other_probs) / len(other_probs)) if other_probs else best_prob,
        consensus_bookmaker_count=len(other_probs),
        fair_price=(1.0 / (sum(other_probs) / len(other_probs))) if other_probs else (1.0 / best_prob),
        edge=((sum(other_probs) / len(other_probs)) - best_prob) if other_probs else 0.0,
        expected_value=(((sum(other_probs) / len(other_probs)) * best["price"]) - 1.0) if other_probs else -1.0,
        bookmaker_count=len(quotes),
        all_prices=[q["price"] for q in quotes],
    )

    if candidate.bookmaker_count < MIN_BOOKMAKERS:
        candidate.rejection_reasons.append(
            f"Только {candidate.bookmaker_count} букмекер(ов) по этому исходу — нужно минимум {MIN_BOOKMAKERS}"
        )
    if candidate.consensus_bookmaker_count == 0:
        candidate.rejection_reasons.append("Нет независимого консенсуса (не с кем сравнить лучшую цену)")
    if candidate.edge < MIN_EDGE:
        candidate.rejection_reasons.append(
            f"Расхождение {candidate.edge:.3f} ниже минимума +{MIN_EDGE:.2f}"
        )
    if candidate.expected_value <= 0:
        candidate.rejection_reasons.append(f"Ожидаемая ценность {candidate.expected_value:.3f} не положительна")

    return candidate


def build_value_candidates_for_event(event: Dict[str, Any], *, event_id: str, league: Optional[str]) -> List[ValueCandidate]:
    """Builds one ValueCandidate per real selection/line found in the raw
    Odds API event for every supported market -- never invents a market or
    selection that was not actually offered by at least one bookmaker."""
    home_team = event.get("home_team", "")
    away_team = event.get("away_team", "")
    match_datetime = event.get("commence_time", "")
    if not home_team or not away_team:
        return []

    candidates: List[ValueCandidate] = []

    for market_key in ("h2h", "double_chance", "draw_no_bet"):
        market_type = MARKET_KEY_TO_TYPE[market_key]
        for selection_name, matcher in _selection_matchers(market_key, home_team, away_team):
            candidate = _build_for_selection(
                event=event, event_id=event_id, league=league,
                market_key=market_key, market_type=market_type,
                selection_name=selection_name, outcome_matcher=matcher,
                home_team=home_team, away_team=away_team, match_datetime=match_datetime,
            )
            if candidate:
                candidates.append(candidate)

    for point in _distinct_totals_points(event):
        for selection_name, matcher in (
            ("Over", lambda name: name.lower() == "over"),
            ("Under", lambda name: name.lower() == "under"),
        ):
            candidate = _build_for_selection(
                event=event, event_id=event_id, league=league,
                market_key="totals", market_type=MARKET_KEY_TO_TYPE["totals"],
                selection_name=f"{selection_name} {point}", outcome_matcher=matcher,
                home_team=home_team, away_team=away_team, match_datetime=match_datetime,
                point=point,
            )
            if candidate:
                candidates.append(candidate)

    return candidates
