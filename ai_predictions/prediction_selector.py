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

import datetime
from dataclasses import dataclass
from typing import List, Optional

from ai_predictions.football_predictions import MarketCandidate
from ai_predictions.value_config import (
    MIN_LEAD_TIME_MINUTES,
    PROB_HIGH_MIN,
    PROB_HIGH_MIN_COMPLETENESS,
    PROB_LOW_MIN,
    PROB_MEDIUM_MIN,
    SIGNAL_HIGH,
    SIGNAL_LOW,
    SIGNAL_MEDIUM,
)

MAX_RECOMMENDATIONS = 5


def classify(probability: float, completeness: float, sample_size_category: str = "strong") -> Optional[str]:
    """Returns HIGH/MEDIUM/LOW, or None if the candidate does not even
    reach the LOW threshold (in which case it must not be shown at all).

    `sample_size_category == "none"` means the candidate carries ZERO
    fixture-specific evidence (the historical-baseline fallback -- see
    football_predictions._historical_baseline_candidates). Such a
    candidate is always capped at LOW, regardless of its raw probability,
    because a generic aggregate statistic (e.g. "home win or draw ~72%
    globally") must never be presented with HIGH/MEDIUM confidence as if
    it were derived from this specific match."""
    if sample_size_category == "none":
        return SIGNAL_LOW if probability >= PROB_LOW_MIN else None
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


def rank_all_candidates(all_candidates: List[MarketCandidate]) -> List[RankedRecommendation]:
    """The FULL ranked pool (best-first), deliberately never sliced to
    MAX_RECOMMENDATIONS here. Request-time re-selection (2026-07-15
    change, see select_current_recommendations below) needs the whole
    pool persisted so a later request -- once some fixtures have already
    kicked off -- can still find real, still-startable candidates that
    did not make the very first cut. Slicing early would permanently
    throw those away."""
    per_fixture = best_candidate_per_fixture(all_candidates)
    ranked: List[RankedRecommendation] = []
    for c in per_fixture:
        level = classify(c.probability, c.completeness, c.sample_size_category)
        if level is None:
            continue
        ranked.append(RankedRecommendation(candidate=c, signal_level=level))

    tier_order = {SIGNAL_HIGH: 0, SIGNAL_MEDIUM: 1, SIGNAL_LOW: 2}
    ranked.sort(key=lambda r: (tier_order[r.signal_level], -r.candidate.probability))
    return ranked


def select_recommendations(all_candidates: List[MarketCandidate]) -> List[RankedRecommendation]:
    """Kept for callers/tests that only care about "the best N candidates
    right now, regardless of kickoff time" (no time-based exclusion) --
    e.g. unit tests that build candidates with no real kickoff constraint
    in mind. The real production pipeline uses rank_all_candidates +
    select_current_recommendations instead, see football_pipeline.py."""
    return rank_all_candidates(all_candidates)[:MAX_RECOMMENDATIONS]


def has_enough_lead_time(
    kickoff_utc: datetime.datetime,
    now: datetime.datetime,
    min_lead_minutes: float = MIN_LEAD_TIME_MINUTES,
) -> bool:
    """True only if the fixture starts strictly after `now` AND at least
    `min_lead_minutes` from now. Excludes matches that have already
    kicked off, already finished, or start too soon for a user to
    realistically still place a bet -- never guessed, always a direct
    comparison against the real kickoff timestamp."""
    return kickoff_utc > now + datetime.timedelta(minutes=min_lead_minutes)


def select_current_recommendations(
    ranked: List[RankedRecommendation],
    now: datetime.datetime,
    *,
    min_lead_minutes: float = MIN_LEAD_TIME_MINUTES,
    max_count: int = MAX_RECOMMENDATIONS,
) -> List[RankedRecommendation]:
    """Re-selects from an already best-first-ranked pool (see
    rank_all_candidates) for THIS exact moment: fixtures that have
    started, finished, or start too soon (< min_lead_minutes away) are
    excluded first, then the best `max_count` of whatever real
    candidates remain are kept. Never pads with a weaker/fake candidate
    just to reach `max_count` -- if fewer real candidates remain after
    the time filter, only those are returned. Order is preserved from
    `ranked`, which must already be best-first sorted."""
    eligible = [
        r for r in ranked
        if has_enough_lead_time(r.candidate.fixture.kickoff_utc, now, min_lead_minutes)
    ]
    return eligible[:max_count]
