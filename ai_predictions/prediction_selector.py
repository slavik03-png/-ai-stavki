"""
Selection/ranking for the API-Football-only candidate list (production
v3). Deliberately separate from ai_predictions/value_selector.py (the
older odds-driven HIGH/MEDIUM/LOW selection, which stays untouched for
the legacy pipeline) because the gating logic here is probability +
completeness based, not edge/EV based.

Rules (exactly the production-fix spec):
- Never invent a fixture or a market: only candidates already built by
  ai_predictions/football_predictions.py are eligible.
- At most one candidate per fixture (its single best real market) --
  keeps the final list one recommendation per real match, matching the
  card format.
- A candidate must reach at least PROB_LOW_MIN to be shown at all.
- HIGH additionally requires PROB_HIGH_MIN_COMPLETENESS.
- Always try to fill up to 5 (globally best-first across fixtures); if
  fewer than 5 real candidates clear the LOW threshold, show only what
  is real -- never pad.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ai_predictions.football_predictions import MarketCandidate
from ai_predictions.value_config import (
    PROB_HIGH_MIN,
    PROB_HIGH_MIN_COMPLETENESS,
    PROB_LOW_MIN,
    PROB_MEDIUM_MIN,
    SIGNAL_HIGH,
    SIGNAL_LOW,
    SIGNAL_MEDIUM,
)

MAX_RECOMMENDATIONS = 5


def classify(probability: float, completeness: float) -> Optional[str]:
    """Returns HIGH/MEDIUM/LOW, or None if the candidate does not even
    reach the LOW threshold (in which case it must not be shown at
    all)."""
    if probability >= PROB_HIGH_MIN and completeness >= PROB_HIGH_MIN_COMPLETENESS:
        return SIGNAL_HIGH
    if probability >= PROB_MEDIUM_MIN:
        return SIGNAL_MEDIUM
    if probability >= PROB_LOW_MIN:
        return SIGNAL_LOW
    return None


@dataclass
class RankedRecommendation:
    candidate: MarketCandidate
    signal_level: str


def best_candidate_per_fixture(all_candidates: List[MarketCandidate]) -> List[MarketCandidate]:
    """Reduces the full per-fixture-per-market candidate list to at most
    one candidate per fixture: the highest real probability among its
    own markets."""
    best_by_fixture = {}
    for c in all_candidates:
        key = c.fixture.fixture_id
        current = best_by_fixture.get(key)
        if current is None or c.probability > current.probability:
            best_by_fixture[key] = c
    return list(best_by_fixture.values())


def select_recommendations(all_candidates: List[MarketCandidate]) -> List[RankedRecommendation]:
    per_fixture = best_candidate_per_fixture(all_candidates)
    ranked: List[RankedRecommendation] = []
    for c in per_fixture:
        level = classify(c.probability, c.completeness)
        if level is None:
            continue
        ranked.append(RankedRecommendation(candidate=c, signal_level=level))

    tier_order = {SIGNAL_HIGH: 0, SIGNAL_MEDIUM: 1, SIGNAL_LOW: 2}
    ranked.sort(key=lambda r: (tier_order[r.signal_level], -r.candidate.probability))
    return ranked[:MAX_RECOMMENDATIONS]
