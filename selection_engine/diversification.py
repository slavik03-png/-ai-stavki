"""
Diversification (spec section 14): avoid near-identical/correlated
markets, cap one MAIN pick per event, and prevent over-concentration in a
single league or market family -- without ever padding the output with
weaker picks just to hit a quota.

This module only removes/demotes candidates for diversity reasons; it
never adds candidates. Input candidates are assumed to already be sorted
by selection_score descending (strongest first), so a simple greedy pass
keeps the strongest representative of each constrained group.
"""

from __future__ import annotations

from typing import Dict, List, Set, Tuple

from selection_engine.config import SelectionConfig, correlation_family
from selection_engine.models import CandidatePrediction


def diversify(
    ranked_candidates: List[CandidatePrediction],
    config: SelectionConfig,
    *,
    max_picks: int,
) -> Tuple[List[CandidatePrediction], List[CandidatePrediction]]:
    """Greedily walks `ranked_candidates` (already sorted strongest-first)
    and keeps up to `max_picks` while enforcing:
      - at most `config.max_main_per_event` picks per event_id
      - at most one pick per correlation family per event_id (avoids
        near-duplicate bets on the same match, e.g. 1X2 + double chance)
      - at most `config.max_main_per_league` picks per league
      - at most `config.max_main_per_market_family` picks per correlation
        family across the whole slate (anti-concentration)

    Returns (kept, dropped_for_diversity). `dropped_for_diversity` candidates
    are NOT rejections -- they were good enough to pass filters but lost out
    to a stronger, competing pick; the selector may still place them in
    RESERVE.
    """
    kept: List[CandidatePrediction] = []
    dropped: List[CandidatePrediction] = []

    per_event_count: Dict[str, int] = {}
    per_event_families: Dict[str, Set[str]] = {}
    per_league_count: Dict[str, int] = {}
    per_family_count: Dict[str, int] = {}

    for candidate in ranked_candidates:
        if len(kept) >= max_picks:
            dropped.append(candidate)
            continue

        event_id = candidate.event_id
        league = candidate.league or "unknown"
        family = correlation_family(candidate.market_type)

        event_count = per_event_count.get(event_id, 0)
        event_families = per_event_families.get(event_id, set())
        league_count = per_league_count.get(league, 0)
        family_count = per_family_count.get(family, 0)

        if event_count >= config.max_main_per_event:
            dropped.append(candidate)
            continue
        if family in event_families:
            dropped.append(candidate)
            continue
        if league_count >= config.max_main_per_league:
            dropped.append(candidate)
            continue
        if family_count >= config.max_main_per_market_family:
            dropped.append(candidate)
            continue

        candidate.correlated_group_id = family
        kept.append(candidate)
        per_event_count[event_id] = event_count + 1
        per_event_families.setdefault(event_id, set()).add(family)
        per_league_count[league] = league_count + 1
        per_family_count[family] = family_count + 1

    return kept, dropped
