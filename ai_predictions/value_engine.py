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

import statistics as _pystats

from ai_predictions.country_map import country_for_sport_key
from ai_predictions.matching import AWAY, DRAW, HOME, OVER, UNDER, MarketGroup
from ai_predictions.value_config import (
    HIGH_MIN_BOOKMAKERS,
    HIGH_MIN_EDGE,
    HIGH_MIN_EV,
    LOW_MIN_BOOKMAKERS,
    LOW_MIN_EDGE,
    LOW_MIN_EV,
    MEDIUM_MIN_BOOKMAKERS,
    MEDIUM_MIN_EDGE,
    MEDIUM_MIN_EV,
    MIN_BEST_ODDS,
    NEAR_BEST_PRICE_PCT,
    OUTLIER_PRICE_GAP_THRESHOLD,
    RANKING_OUTLIER_PENALTY,
    RANKING_WEIGHT_BOOKMAKERS,
    RANKING_WEIGHT_DISPERSION_PENALTY,
    RANKING_WEIGHT_EDGE,
    RANKING_WEIGHT_EV,
    SIGNAL_HIGH,
    SIGNAL_LOW,
    SIGNAL_MEDIUM,
    SIGNAL_REJECTED,
    STATS_BLEND_MAGNITUDE,
)
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
#: Backward-compatible aliases -- the strategy used to have one fixed
#: binary threshold; the ranked system replaces it with three per-level
#: thresholds in ai_predictions/value_config.py. These aliases are kept
#: only so any external reference to "the minimum bar" resolves to the
#: loosest one still capable of producing a signal (LOW), never silently
#: to a stricter one.
MIN_BOOKMAKERS = LOW_MIN_BOOKMAKERS
MIN_EDGE = LOW_MIN_EDGE

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
    expected_value: float  # consensus_probability * best_price - 1 (fair_probability * offered_odds - 1)

    bookmaker_count: int  # unique bookmakers quoting this exact selection/line (including the best one)
    all_prices: List[float] = field(default_factory=list)

    # -- Step 3 confidence safeguards: real, observable market-shape stats --
    unique_bookmaker_count: int = 0
    total_bookmaker_rows_before_dedup: int = 0
    median_price: float = 0.0
    mean_price: float = 0.0
    min_price: float = 0.0
    max_price: float = 0.0
    price_dispersion: float = 0.0  # population stdev of all_prices
    best_second_gap: float = 0.0  # (best - second_best) / second_best; 0 if no second price
    near_best_count: int = 0  # bookmakers within NEAR_BEST_PRICE_PCT of the best price
    is_outlier: bool = False
    outlier_warning: Optional[str] = None
    data_quality_warnings: List[str] = field(default_factory=list)

    # -- Step 2 ranking outcome --
    signal_level: str = SIGNAL_REJECTED
    ranking_score: float = 0.0

    rejection_reasons: List[str] = field(default_factory=list)
    generated_at: str = ""

    # -- API-Football statistics enrichment (optional; None/defaults mean
    #    "never attempted", not "attempted and failed" -- see
    #    ai_predictions/enrichment.py). Never changes signal_level; only
    #    re-ranks within the already-decided tier via final_combined_score. --
    statistics_source: str = "not_attempted"  # not_attempted|api_football|unavailable|unmatched|quota_reserved
    statistics_cached: bool = False
    statistics_completeness: float = 0.0  # 0..1, how much of the needed real data was retrieved
    statistics_score: Optional[float] = None  # 0..1, 0.5 = neutral/no opinion; None until computed
    final_combined_score: Optional[float] = None  # ranking_score nudged by statistics_score; None if never enriched

    def __post_init__(self) -> None:
        if not self.generated_at:
            self.generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    @property
    def passed(self) -> bool:
        """Backward-compatible alias: "passed" now means "produced any
        real signal level, not REJECTED" -- kept for any external code
        still checking a binary pass/fail."""
        return self.signal_level != SIGNAL_REJECTED


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


def _price_shape_stats(prices: List[float]) -> Dict[str, Any]:
    """Real, observable descriptive statistics over one outcome's own
    bookmaker prices (Step 3 confidence safeguards) -- no probability or
    edge math here, just plain price-shape facts."""
    ordered = sorted(prices, reverse=True)
    best = ordered[0]
    second = ordered[1] if len(ordered) > 1 else None
    best_second_gap = ((best - second) / second) if second and second > 0 else 0.0
    near_best_count = sum(1 for p in prices if p >= best * (1.0 - NEAR_BEST_PRICE_PCT))
    dispersion = _pystats.pstdev(prices) if len(prices) > 1 else 0.0
    is_outlier = best_second_gap > OUTLIER_PRICE_GAP_THRESHOLD if second is not None else False
    return {
        "median_price": _pystats.median(prices),
        "mean_price": _pystats.fmean(prices),
        "min_price": min(prices),
        "max_price": max(prices),
        "price_dispersion": dispersion,
        "best_second_gap": best_second_gap,
        "near_best_count": near_best_count,
        "is_outlier": is_outlier,
    }


def classify_signal(candidate: ValueCandidate) -> "tuple[str, List[str], Optional[str]]":
    """Assigns exactly one of HIGH/MEDIUM/LOW/REJECTED (Step 2 + Step 3 +
    Step 4 of the spec). Checks HIGH's complete condition set first, then
    MEDIUM's, then LOW's -- the first fully-satisfied level wins, so a
    candidate meeting HIGH is never also shown as MEDIUM or LOW. Returns
    (level, rejection_reasons, outlier_warning_text). An outlier warning
    always demotes the level by exactly one step, cascading a would-be
    LOW down to REJECTED (Step 3/Step 4)."""
    reasons: List[str] = []
    warnings = list(candidate.data_quality_warnings)

    consensus_valid = candidate.consensus_bookmaker_count > 0
    if not consensus_valid:
        reasons.append("Нет независимого консенсуса (не с кем сравнить лучшую цену)")
    if candidate.best_price <= MIN_BEST_ODDS:
        reasons.append(f"Лучшая цена {candidate.best_price:.2f} не выше минимума {MIN_BEST_ODDS:.2f}")
    if warnings:
        reasons.append(f"Критическое предупреждение о качестве данных: {'; '.join(warnings)}")

    base_ok = consensus_valid and candidate.best_price > MIN_BEST_ODDS and not warnings

    outlier_warning = None
    if candidate.is_outlier:
        outlier_warning = (
            f"Лучшая цена {candidate.best_price:.2f} более чем на "
            f"{OUTLIER_PRICE_GAP_THRESHOLD * 100:.0f}% выше второй лучшей цены — возможен изолированный выброс."
        )

    def meets(min_bm: int, min_ev: float, min_edge: float) -> bool:
        return (
            base_ok
            and candidate.unique_bookmaker_count >= min_bm
            and candidate.expected_value >= min_ev
            and candidate.edge >= min_edge
        )

    if meets(HIGH_MIN_BOOKMAKERS, HIGH_MIN_EV, HIGH_MIN_EDGE):
        level = SIGNAL_HIGH
    elif meets(MEDIUM_MIN_BOOKMAKERS, MEDIUM_MIN_EV, MEDIUM_MIN_EDGE):
        level = SIGNAL_MEDIUM
    elif meets(LOW_MIN_BOOKMAKERS, LOW_MIN_EV, LOW_MIN_EDGE):
        level = SIGNAL_LOW
    else:
        level = SIGNAL_REJECTED

    # Step 4: two-bookmaker markets can never be HIGH -- already guaranteed
    # above since HIGH requires unique_bookmaker_count >= HIGH_MIN_BOOKMAKERS
    # (3), so a 2-bookmaker candidate can only ever reach MEDIUM or LOW.

    # Outlier cascade: demotes exactly one level, HIGH->MEDIUM->LOW->REJECTED.
    _CASCADE = {SIGNAL_HIGH: SIGNAL_MEDIUM, SIGNAL_MEDIUM: SIGNAL_LOW, SIGNAL_LOW: SIGNAL_REJECTED}
    if outlier_warning and level in _CASCADE:
        demoted_from = level
        level = _CASCADE[level]
        reasons.append(
            f"Понижено с {demoted_from} до {level} из-за предупреждения о выбросе в цене"
            if level != SIGNAL_REJECTED
            else f"Понижено с {demoted_from} до REJECTED из-за предупреждения о выбросе в цене"
        )

    if level == SIGNAL_REJECTED and not reasons:
        if candidate.unique_bookmaker_count < LOW_MIN_BOOKMAKERS:
            reasons.append(
                f"Только {candidate.unique_bookmaker_count} букмекер(ов) по этому исходу — нужно минимум {LOW_MIN_BOOKMAKERS}"
            )
        if candidate.expected_value < LOW_MIN_EV:
            reasons.append(f"Ожидаемая ценность {candidate.expected_value:.3f} ниже минимума +{LOW_MIN_EV:.2f}")
        if candidate.edge < LOW_MIN_EDGE:
            reasons.append(f"Расхождение {candidate.edge:.3f} ниже минимума +{LOW_MIN_EDGE:.2f}")

    if candidate.unique_bookmaker_count == 2 and level in (SIGNAL_MEDIUM, SIGNAL_LOW):
        reasons.append("Только 2 независимых букмекера — сниженная уверенность.")

    return level, reasons, outlier_warning


def compute_ranking_score(candidate: ValueCandidate) -> float:
    """Transparent ranking score (Step 6): rewards real EV and real edge,
    rewards broader independent bookmaker coverage (diminishing via log so
    a market with 40 bookmakers doesn't automatically dominate one with 6
    just by being more heavily covered), penalizes price dispersion
    (a wide spread across bookmakers means less agreement on the "true"
    price) and applies a flat penalty when an outlier warning is present.
    A very high quoted price alone never dominates -- price only enters
    indirectly through EV/edge, which are already probability-normalized.

    score = EV * W_EV
          + edge * W_EDGE
          + log2(unique_bookmakers) * W_BOOKMAKERS
          - (price_dispersion / max(mean_price, 1)) * W_DISPERSION
          - (W_OUTLIER if is_outlier else 0)
    """
    import math

    bookmaker_term = math.log2(max(candidate.unique_bookmaker_count, 1) + 1) * RANKING_WEIGHT_BOOKMAKERS
    dispersion_term = 0.0
    if candidate.mean_price > 0:
        dispersion_term = (candidate.price_dispersion / candidate.mean_price) * RANKING_WEIGHT_DISPERSION_PENALTY
    outlier_term = RANKING_OUTLIER_PENALTY if candidate.is_outlier else 0.0

    score = (
        candidate.expected_value * RANKING_WEIGHT_EV
        + candidate.edge * RANKING_WEIGHT_EDGE
        + bookmaker_term
        - dispersion_term
        - outlier_term
    )
    return round(score, 4)


def compute_combined_score(candidate: ValueCandidate) -> float:
    """Blends the real statistics-agreement signal (if enrichment ever ran
    for this candidate) into ranking_score, purely as a within-tier
    re-ranking nudge (Step: combined scoring). candidate.statistics_score
    is None whenever enrichment was never attempted or produced nothing
    usable -- in that exact case this returns ranking_score completely
    unchanged, so an odds-only run behaves identically to before this
    feature existed."""
    if candidate.statistics_score is None:
        return candidate.ranking_score
    agreement = candidate.statistics_score - 0.5  # -0.5 (disagrees) .. +0.5 (agrees)
    nudge = agreement * STATS_BLEND_MAGNITUDE * max(candidate.statistics_completeness, 0.0)
    return round(candidate.ranking_score + nudge, 4)


def _build_candidate_for_outcome(group: MarketGroup, canonical: str,
                                  raw_counts: Optional[Dict[Any, int]] = None) -> Optional[ValueCandidate]:
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
    all_prices = [q["price"] for q in quotes]
    unique_bookmakers = len({q["bookmaker"] for q in quotes})
    shape = _price_shape_stats(all_prices)

    data_quality_warnings: List[str] = []
    if len(best["complete_prices"]) < 2:
        data_quality_warnings.append("Неполный набор исходов рынка у лучшей цены — маржа не может быть удалена корректно")

    raw_count = unique_bookmakers
    if raw_counts is not None:
        raw_key = (group.event_key, group.market, group.point, canonical)
        raw_count = raw_counts.get(raw_key, unique_bookmakers)

    candidate = ValueCandidate(
        event_id=group.event_id,
        sport="soccer",
        league=group.league,
        country=country_for_sport_key(group.event_key[0]),
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
        bookmaker_count=unique_bookmakers,
        all_prices=all_prices,
        unique_bookmaker_count=unique_bookmakers,
        total_bookmaker_rows_before_dedup=raw_count,
        median_price=shape["median_price"],
        mean_price=shape["mean_price"],
        min_price=shape["min_price"],
        max_price=shape["max_price"],
        price_dispersion=shape["price_dispersion"],
        best_second_gap=shape["best_second_gap"],
        near_best_count=shape["near_best_count"],
        is_outlier=shape["is_outlier"],
        data_quality_warnings=data_quality_warnings,
    )

    level, reasons, outlier_warning = classify_signal(candidate)
    candidate.signal_level = level
    candidate.rejection_reasons = reasons
    candidate.outlier_warning = outlier_warning
    candidate.ranking_score = compute_ranking_score(candidate)

    return candidate


def build_value_candidates_from_groups(
    groups: Dict[Any, MarketGroup],
    raw_counts: Optional[Dict[Any, int]] = None,
) -> List[ValueCandidate]:
    """Builds one ValueCandidate per real (event, market, point, outcome)
    group -- never invents a market or selection that no bookmaker actually
    offered, since groups only exist for outcomes matching.py actually saw.
    `raw_counts`, if given, maps (event_key, market, point, canonical) ->
    total bookmaker rows seen for that outcome before bookmaker-level
    deduplication (Step 3's "total bookmaker count before deduplication")."""
    candidates: List[ValueCandidate] = []
    for group in groups.values():
        for canonical in group.outcomes.keys():
            candidate = _build_candidate_for_outcome(group, canonical, raw_counts)
            if candidate:
                candidates.append(candidate)
    return candidates
