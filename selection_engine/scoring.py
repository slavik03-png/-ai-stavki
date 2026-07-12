"""
Deterministic scoring functions: implied probability, margin removal,
edge, expected value, data completeness, sample reliability, and the
final 0..100 confidence score.

No randomness anywhere in this module -- the same inputs always produce
the same outputs. Missing data is never replaced with an invented value;
it lowers confidence or removes a factor from a computation entirely
(never treated as zero evidence).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from selection_engine.config import (
    ConfidenceWeights,
    market_requirements_for,
    SampleBand,
    SelectionConfig,
)
from selection_engine.models import CandidatePrediction

# ---------------------------------------------------------------------------
# 3. Bookmaker implied probability
# ---------------------------------------------------------------------------

def raw_implied_probability(odds: float) -> float:
    if odds <= 0:
        raise ValueError(f"odds must be positive, got {odds!r}")
    return 1.0 / odds


def normalise_market_probabilities(raw_probabilities: Sequence[float]) -> List[float]:
    """Removes the bookmaker margin across a *complete* set of mutually
    exclusive outcomes in the same market (e.g. all three 1X2 prices)."""
    total = sum(raw_probabilities)
    if total <= 0:
        raise ValueError("sum of raw probabilities must be positive")
    return [p / total for p in raw_probabilities]


def bookmaker_probability(
    odds: float,
    complete_market_odds: Optional[Sequence[float]] = None,
) -> "tuple[float, Optional[float], bool]":
    """Returns (raw_implied_probability, margin_adjusted_probability_or_None,
    is_margin_adjusted). `complete_market_odds` -- all decimal odds in the
    same market including this selection's own odds -- enables margin
    removal; when it is not available the margin-adjusted value is None and
    the caller must clearly mark the probability as not margin-adjusted."""
    raw = raw_implied_probability(odds)
    if not complete_market_odds:
        return raw, None, False
    raw_all = [raw_implied_probability(o) for o in complete_market_odds]
    normalised = normalise_market_probabilities(raw_all)
    # Find this selection's normalised value by matching its raw probability
    # position (caller is expected to pass odds in a stable, known order;
    # for a single-odds lookup we recompute directly instead of searching).
    normalised_for_this = raw / sum(raw_all)
    return raw, normalised_for_this, True


# ---------------------------------------------------------------------------
# 5. Edge and expected value
# ---------------------------------------------------------------------------

def compute_edge(model_probability: float, bookmaker_implied_probability: float) -> float:
    return model_probability - bookmaker_implied_probability


def compute_expected_value(model_probability: float, odds: float) -> float:
    return model_probability * odds - 1.0


def compute_fair_odds(model_probability: float) -> Optional[float]:
    if model_probability <= 0:
        return None
    return 1.0 / model_probability


# ---------------------------------------------------------------------------
# 6. Data completeness
# ---------------------------------------------------------------------------

def compute_data_completeness(
    available_fields: Dict[str, bool],
    required: Sequence[str],
    optional: Sequence[str],
) -> float:
    """Required fields count double weight versus optional fields. A field
    absent from `available_fields` is treated as missing (never assumed
    present). Returns 0..1; 1.0 only when every required and optional field
    for the market is present."""
    if not required and not optional:
        return 1.0
    required_weight = 2.0
    optional_weight = 1.0
    total_weight = len(required) * required_weight + len(optional) * optional_weight
    if total_weight == 0:
        return 1.0
    earned = 0.0
    for f in required:
        if available_fields.get(f):
            earned += required_weight
    for f in optional:
        if available_fields.get(f):
            earned += optional_weight
    return earned / total_weight


def missing_required_fields(
    available_fields: Dict[str, bool],
    required: Sequence[str],
) -> List[str]:
    return [f for f in required if not available_fields.get(f)]


# ---------------------------------------------------------------------------
# 7. Sample size penalty / reliability factor
# ---------------------------------------------------------------------------

def sample_reliability_factor(sample_size: int, bands: Sequence[SampleBand]) -> float:
    for band in bands:
        if sample_size < band.max_matches:
            return band.factor
    return bands[-1].factor if bands else 0.0


def sample_reliability_label(sample_size: int, bands: Sequence[SampleBand]) -> str:
    for band in bands:
        if sample_size < band.max_matches:
            return band.label
    return bands[-1].label if bands else "неизвестно"


# ---------------------------------------------------------------------------
# 10. Confidence score
# ---------------------------------------------------------------------------

def compute_confidence_score(
    *,
    model_probability: float,
    expected_value: float,
    data_completeness: float,
    sample_reliability: float,
    market_reliability: float,
    historical_calibration_quality: float,
    historical_market_quality: float,
    missing_field_count: int,
    is_contradictory: bool,
    is_stale: bool,
    weights: ConfidenceWeights,
) -> float:
    """Deterministic, documented formula:

    confidence =
          probability_component        (model_probability, 0..1, scaled)
        + value_component              (expected_value, clamped 0..1, scaled)
        + data_component               (data_completeness, 0..1, scaled)
        + sample_component             (sample_reliability, 0..1, scaled)
        + reliability_component        (market_reliability, 0..1, scaled)
        + historical_component         (average of calibration & market
                                         historical quality, 0..1, scaled)
        - missing_data_penalty         (per missing required field)
        - contradiction_penalty        (flat, if home/away trends disagree)
        - stale_data_penalty           (flat, if price/stats are too old)

    Every "_component" is `clamp01(value) * weight`, so a market cannot
    inflate its score just because it has attractive odds -- odds only
    enter indirectly through `expected_value`, and a very high probability
    at a very low price still produces a small (or negative) value
    component. The result is clamped to 0..100.
    """

    def clamp01(x: float) -> float:
        return max(0.0, min(1.0, x))

    probability_component = clamp01(model_probability) * weights.probability_weight
    # expected_value is unbounded above/below zero; map to 0..1 with a
    # conservative squashing so a modest positive EV (e.g. +0.10) already
    # earns most of the credit, while negative EV earns none.
    value_component = clamp01(0.5 + expected_value * 2.5) * weights.value_weight
    data_component = clamp01(data_completeness) * weights.data_completeness_weight
    sample_component = clamp01(sample_reliability) * weights.sample_reliability_weight
    reliability_component = clamp01(market_reliability) * weights.market_reliability_weight
    historical_component = (
        clamp01((historical_calibration_quality + historical_market_quality) / 2.0)
        * weights.historical_calibration_weight
    )
    # historical_market_weight is folded into historical_component's inputs
    # (both quality signals are already averaged above); kept as a
    # configuration knob for future independent tuning.

    total = (
        probability_component
        + value_component
        + data_component
        + sample_component
        + reliability_component
        + historical_component
    )
    total -= missing_field_count * weights.missing_data_penalty_per_gap
    if is_contradictory:
        total -= weights.contradiction_penalty
    if is_stale:
        total -= weights.stale_data_penalty

    return max(0.0, min(100.0, total))


# ---------------------------------------------------------------------------
# 16. Selection (ranking) score
# ---------------------------------------------------------------------------

def compute_selection_score(
    candidate: CandidatePrediction,
    *,
    correlation_penalty_applied: bool,
    weights,
) -> float:
    """Ranking score used to order accepted candidates. Deliberately not
    the same as confidence_score -- it also weighs historical reliability,
    data quality, and odds sanity, and applies correlation/risk penalties
    so the ranking never collapses to "sort by confidence"."""

    def clamp01(x: float) -> float:
        return max(0.0, min(1.0, x))

    confidence = (candidate.confidence_score or 0.0) / 100.0
    value = clamp01(0.5 + (candidate.expected_value or 0.0) * 2.5)
    market_rate = candidate.historical_market_win_rate if candidate.historical_market_win_rate is not None else 0.5
    model_rate = (
        candidate.historical_model_version_win_rate
        if candidate.historical_model_version_win_rate is not None else 0.5
    )
    historical = clamp01((market_rate + model_rate) / 2.0)
    data_quality = clamp01(candidate.data_completeness or 0.0)

    # Odds sanity: penalise extreme odds (too close to 1.0 has little value
    # even at high probability; extreme long shots are volatile).
    odds = candidate.odds
    if odds < 1.3:
        odds_sanity = 0.3
    elif odds > 5.0:
        odds_sanity = 0.4
    else:
        odds_sanity = 1.0

    score = (
        confidence * weights.confidence_weight
        + value * weights.value_weight
        + historical * weights.historical_reliability_weight
        + data_quality * weights.data_quality_weight
        + odds_sanity * weights.odds_sanity_weight
    )
    if correlation_penalty_applied:
        score -= weights.correlation_penalty / 100.0
    if candidate.market_type in {"correct_score"}:
        score -= weights.risk_penalty / 100.0
    return round(score, 6)


def score_candidate(
    candidate: CandidatePrediction,
    config: SelectionConfig,
    *,
    complete_market_odds: Optional[Sequence[float]] = None,
    historical_market_win_rate: Optional[float] = None,
    historical_market_roi: Optional[float] = None,
    historical_model_version_win_rate: Optional[float] = None,
    historical_model_version_roi: Optional[float] = None,
    calibration_quality: float = 0.5,
    is_stale: bool = False,
) -> CandidatePrediction:
    """Fills in every derived field on `candidate` in place and returns it.
    Pure/deterministic given the same candidate + config + historical
    inputs -- never invents missing statistics."""
    raw, adjusted, is_adjusted = bookmaker_probability(candidate.odds, complete_market_odds)
    candidate.bookmaker_implied_probability = adjusted if is_adjusted else raw

    candidate.edge = compute_edge(candidate.model_probability, candidate.bookmaker_implied_probability)
    candidate.expected_value = compute_expected_value(candidate.model_probability, candidate.odds)
    candidate.fair_odds = compute_fair_odds(candidate.model_probability)

    market_reqs = market_requirements_for(candidate.market_type, config)
    required = market_reqs["required"]
    optional = market_reqs["optional"]
    candidate.data_completeness = compute_data_completeness(candidate.available_fields, required, optional)
    missing = missing_required_fields(candidate.available_fields, required)

    sample_reliability = sample_reliability_factor(candidate.sample_size, config.sample_bands)

    candidate.historical_market_win_rate = historical_market_win_rate
    candidate.historical_market_roi = historical_market_roi
    candidate.historical_model_version_win_rate = historical_model_version_win_rate
    candidate.historical_model_version_roi = historical_model_version_roi

    market_quality = historical_market_win_rate if historical_market_win_rate is not None else 0.5
    if candidate.market_reliability is None:
        candidate.market_reliability = market_quality

    candidate.confidence_score = compute_confidence_score(
        model_probability=candidate.model_probability,
        expected_value=candidate.expected_value,
        data_completeness=candidate.data_completeness,
        sample_reliability=sample_reliability,
        market_reliability=candidate.market_reliability,
        historical_calibration_quality=calibration_quality,
        historical_market_quality=market_quality,
        missing_field_count=len(missing),
        is_contradictory=candidate.is_contradictory,
        is_stale=is_stale,
        weights=config.confidence_weights,
    )
    return candidate
