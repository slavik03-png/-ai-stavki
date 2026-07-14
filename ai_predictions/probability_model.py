"""
Phase 6 -- auditable market + statistics probability blend.

Pure, deterministic functions only: no network calls, no randomness.
Given a market-implied probability (already computed by the existing
leave-one-out consensus in value_engine.py) and, when real recent-form
data was retrieved for both teams, a statistics-implied probability, this
module blends the two using a sample-size-dependent weight table so a
tiny/noisy sample can never dominate the estimate and missing data is
never treated as neutral evidence *for* anything -- a candidate with no
usable statistics simply stays market-only (statistics_weight = 0).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ai_predictions.value_config import (
    MEDIUM_SAMPLE_MIN_MATCHES,
    PROBABILITY_BLEND_WEIGHTS,
    PROBABILITY_CLAMP_MAX,
    PROBABILITY_CLAMP_MIN,
    STRONG_SAMPLE_MIN_MATCHES,
    WEAK_SAMPLE_MIN_MATCHES,
)


def sample_size_category(home_matches: int, away_matches: int) -> str:
    """The category is capped by the *weaker* of the two teams' real
    sample sizes -- a strong sample for one side never compensates for a
    thin sample on the other."""
    smaller = min(home_matches, away_matches)
    if smaller >= STRONG_SAMPLE_MIN_MATCHES:
        return "strong"
    if smaller >= MEDIUM_SAMPLE_MIN_MATCHES:
        return "medium"
    if smaller >= WEAK_SAMPLE_MIN_MATCHES:
        return "weak"
    return "none"


def statistics_probability_for_side(home_win_rate: Optional[float], away_win_rate: Optional[float],
                                     side: str) -> Optional[float]:
    """`home_win_rate`/`away_win_rate` are 0..1 win rates from real recent
    form (already divided by 100 -- see tracking-stats-probability-scale
    lesson). `side` is 'home' or 'away'. Returns None when there is no
    usable statistical opinion (both rates zero/missing) -- never guesses
    a neutral 0.5 as if it were evidence."""
    if home_win_rate is None or away_win_rate is None:
        return None
    total = home_win_rate + away_win_rate
    if total <= 0:
        return None
    if side == "home":
        return home_win_rate / total
    if side == "away":
        return away_win_rate / total
    return None


@dataclass
class BlendResult:
    estimated_probability: float
    market_probability: float
    statistics_probability: Optional[float]
    sample_size_category: str
    market_weight: float
    statistics_weight: float


def blend_probability(
    market_probability: float,
    statistics_probability: Optional[float],
    home_matches: int,
    away_matches: int,
) -> BlendResult:
    """Combines the market-implied probability with a statistics-implied
    probability (when available) using the sample-size weight table.
    Falls back to market-only (weight 1.0/0.0) whenever statistics is
    unavailable for this candidate's selection (e.g. draw/totals markets,
    or teams with zero real matches retrieved)."""
    category = sample_size_category(home_matches, away_matches)
    if statistics_probability is None:
        category = "none"
    market_weight, statistics_weight = PROBABILITY_BLEND_WEIGHTS[category]

    if statistics_probability is None:
        blended = market_probability
    else:
        blended = market_weight * market_probability + statistics_weight * statistics_probability

    blended = max(PROBABILITY_CLAMP_MIN, min(PROBABILITY_CLAMP_MAX, blended))

    return BlendResult(
        estimated_probability=blended,
        market_probability=market_probability,
        statistics_probability=statistics_probability,
        sample_size_category=category,
        market_weight=market_weight,
        statistics_weight=statistics_weight,
    )
