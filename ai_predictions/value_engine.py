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

Row extraction, validation, deduplication and grouping (turning a raw
Odds API event into stable per-event/market/point/outcome groups with a
real bookmaker count each) live in ai_predictions/matching.py -- this
module only consumes MarketGroup objects and computes the value-detection
math on top of them.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ai_predictions.matching import AWAY, DRAW, HOME, OVER, UNDER, MarketGroup
from selection_engine.config import (
    MARKET_1X2,
    MARKET_DOUBLE_CHANCE,
    MARKET_DRAW_NO_BET,
    MARKET_SPREAD,
    MARKET_TOTAL_GOALS,
)
from selection_engine.scoring import normalise_market_probabilities, raw_implied_probability

#: matching.py market key -> our internal market_type (kept identical to
#: tracking.settlement's market types so every saved prediction can later
#: be graded).
MARKET_KEY_TO_TYPE: Dict[str, str] = {
    "h2h": MARKET_1X2,
    "double_chance": MARKET_DOUBLE_CHANCE,
    "draw_no_bet": MARKET_DRAW_NO_BET,
    "totals": MARKET_TOTAL_GOALS,
    "spreads": MARKET_SPREAD,
}

#: Human-readable selection label per market/canonical-outcome, used only
#: for display (grouping/matching always uses the canonical label).
_SELECTION_LABELS = {
    "h2h": {HOME: "{home}", DRAW: "Ничья", AWAY: "{away}"},
    "draw_no_bet": {HOME: "{home}", AWAY: "{away}"},
    "spreads": {HOME: "{home}", AWAY: "{away}"},
    "totals": {OVER: "Over", UNDER: "Under"},
    "double_chance": {
        "HOME_OR_DRAW": "{home} или ничья",
        "DRAW_OR_AWAY": "ничья или {away}",
        "HOME_OR_AWAY": "{home} или {away}",
    },
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
    total = sum(raw_all)
    if total <= 0:
        return None
    raw = raw_implied_probability(price)
    return raw / total


def _display_selection(market_key: str, canonical: str, home_team: str, away_team: str) -> str:
    template = _SELECTION_LABELS.get(market_key, {}).get(canonical, canonical)
    return template.format(home=home_team, away=away_team)


def _build_candidate_for_outcome(group: MarketGroup, canonical: str) -> Optional[ValueCandidate]:
    """Consumes one MarketGroup's already-deduplicated (bookmaker, price)
    pairs for one canonical outcome and computes the leave-one-out
    consensus vs. the real best price. Returns None only if there is
    nothing at all to build from (should not happen once matching.py has
    produced the group, but guards against an empty outcome list)."""
    market_key = group.market
    by_bookmaker: Dict[str, Dict[str, float]] = {}
    for outcome_key, entries in group.outcomes.items():
        for bookmaker, price, _point in entries:
            by_bookmaker.setdefault(bookmaker, {})[outcome_key] = price

    quotes = []
    for bookmaker, price, original_point in group.outcomes.get(canonical, []):
        complete_prices = list(by_bookmaker.get(bookmaker, {}).values())
        quotes.append({
            "bookmaker": bookmaker, "price": price, "complete_prices": complete_prices,
            "point": original_point,
        })

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

    consensus = (sum(other_probs) / len(other_probs)) if other_probs else best_prob

    candidate = ValueCandidate(
        event_id=group.event_id,
        sport="soccer",
        league=group.league,
        country=None,
        match_datetime=group.commence_time,
        home_team=group.home_team,
        away_team=group.away_team,
        market_type=MARKET_KEY_TO_TYPE.get(market_key, market_key),
        selection=_display_selection(market_key, canonical, group.home_team, group.away_team),
        line=best["point"] if market_key == "spreads" else group.point,
        best_bookmaker=best["bookmaker"],
        best_price=best["price"],
        best_price_implied_probability=best_prob,
        consensus_probability=consensus,
        consensus_bookmaker_count=len(other_probs),
        fair_price=(1.0 / consensus) if consensus > 0 else float("inf"),
        edge=(consensus - best_prob) if other_probs else 0.0,
        expected_value=((consensus * best["price"]) - 1.0) if other_probs else -1.0,
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


def build_value_candidates_from_groups(groups: Dict[Any, MarketGroup]) -> List[ValueCandidate]:
    """Builds one ValueCandidate per real (event, market, point, outcome)
    group -- never invents a market or selection that no bookmaker actually
    offered, since groups only exist for outcomes matching.py actually saw."""
    candidates: List[ValueCandidate] = []
    for group in groups.values():
        for canonical in group.outcomes.keys():
            candidate = _build_candidate_for_outcome(group, canonical)
            if candidate:
                candidates.append(candidate)
    return candidates
