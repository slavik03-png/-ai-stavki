"""
Selection logic for the ranked HIGH/MEDIUM/LOW/REJECTED value-signal
system (see ai_predictions/value_engine.py for how each ValueCandidate's
signal_level and ranking_score are computed).

Rules (all from the spec, not invented):
- candidates are already leveled by value_engine.classify_signal before
  they reach this module -- this module only dedupes, ranks and caps;
- at most one displayed signal per event, UNLESS two signals concern
  clearly different markets and BOTH pass MEDIUM or HIGH -- a LOW never
  gets a second slot on the same event;
- candidates are then ranked GLOBALLY across every level and event --
  HIGH always before MEDIUM before LOW (never blended by score across
  tiers), score-ordered within a tier -- and only the top MAX_TOTAL_SIGNALS
  survive; nothing is padded to reach that number;
- if zero signals qualify at all, the closest REJECTED candidates (by
  ranking_score, i.e. how close they came to a real threshold) are kept
  separately so the report can show *why* nothing qualified with real
  numbers instead of an empty screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from ai_predictions.value_config import (
    MAX_TOTAL_SIGNALS,
    SIGNAL_HIGH,
    SIGNAL_LOW,
    SIGNAL_MEDIUM,
    SIGNAL_REJECTED,
)
from ai_predictions.value_engine import ValueCandidate

_MULTI_MARKET_ELIGIBLE_LEVELS = (SIGNAL_HIGH, SIGNAL_MEDIUM)
_TIER_RANK = {SIGNAL_HIGH: 0, SIGNAL_MEDIUM: 1, SIGNAL_LOW: 2}


def _sort_key(candidate: ValueCandidate):
    """HIGH always sorts before MEDIUM before LOW (tier is the primary
    key); within a tier, higher ranking_score first."""
    return (_TIER_RANK[candidate.signal_level], -candidate.ranking_score)


@dataclass
class ValueSelectionResult:
    #: Up to MAX_TOTAL_SIGNALS candidates, globally ranked HIGH -> MEDIUM
    #: -> LOW (then by ranking_score within a tier). This is the single
    #: list the report shows -- never split into separate per-level caps.
    top_signals: List[ValueCandidate] = field(default_factory=list)
    #: Every candidate that did NOT make top_signals, for any reason
    #: (genuinely REJECTED by value_engine, bumped by per-event dedup, or
    #: outranked for the last global slot) -- always fully persisted to
    #: tracking regardless.
    rejected: List[ValueCandidate] = field(default_factory=list)
    #: The 5 REJECTED candidates that came closest to qualifying (highest
    #: ranking_score among genuinely REJECTED candidates), shown only when
    #: top_signals is empty (Step 11).
    closest_rejected: List[ValueCandidate] = field(default_factory=list)

    @property
    def high(self) -> List[ValueCandidate]:
        return [c for c in self.top_signals if c.signal_level == SIGNAL_HIGH]

    @property
    def medium(self) -> List[ValueCandidate]:
        return [c for c in self.top_signals if c.signal_level == SIGNAL_MEDIUM]

    @property
    def low(self) -> List[ValueCandidate]:
        return [c for c in self.top_signals if c.signal_level == SIGNAL_LOW]

    @property
    def main(self) -> List[ValueCandidate]:
        """Backward-compatible alias for the flat displayed list."""
        return self.top_signals

    @property
    def all_displayed(self) -> List[ValueCandidate]:
        return self.top_signals


def _dedupe_per_event(candidates: List[ValueCandidate]) -> "tuple[List[ValueCandidate], List[ValueCandidate]]":
    """At most one signal per event, except a second signal is allowed
    when it is on a clearly different market AND both candidates are
    MEDIUM or HIGH. Works across ALL tiers at once (not level-by-level),
    so the strongest signal for an event always wins regardless of which
    tier the competing candidates landed in."""
    by_event: Dict[str, List[ValueCandidate]] = {}
    for c in candidates:
        by_event.setdefault(c.event_id, []).append(c)

    kept: List[ValueCandidate] = []
    bumped: List[ValueCandidate] = []
    for event_id, group in by_event.items():
        group = sorted(group, key=_sort_key)
        best = group[0]
        kept.append(best)
        for extra in group[1:]:
            different_market = extra.market_type != best.market_type
            both_strong = (
                extra.signal_level in _MULTI_MARKET_ELIGIBLE_LEVELS
                and best.signal_level in _MULTI_MARKET_ELIGIBLE_LEVELS
            )
            if different_market and both_strong:
                kept.append(extra)
            else:
                extra.rejection_reasons.append(
                    f"По этому событию уже выбран более сильный сигнал "
                    f"({best.selection}, {best.signal_level}, score {best.ranking_score:.2f})"
                )
                bumped.append(extra)
    return kept, bumped


def select_value_recommendations(candidates: List[ValueCandidate]) -> ValueSelectionResult:
    rejected: List[ValueCandidate] = [c for c in candidates if c.signal_level == SIGNAL_REJECTED]
    non_rejected: List[ValueCandidate] = [c for c in candidates if c.signal_level != SIGNAL_REJECTED]

    kept, bumped = _dedupe_per_event(non_rejected)
    rejected.extend(bumped)

    ranked = sorted(kept, key=_sort_key)
    top = ranked[:MAX_TOTAL_SIGNALS]
    overflow = ranked[MAX_TOTAL_SIGNALS:]
    for extra in overflow:
        extra.rejection_reasons.append(
            f"Не попал в общий топ-{MAX_TOTAL_SIGNALS} сигналов — есть более сильные "
            f"сигналы по другим событиям (приоритет HIGH \u2192 MEDIUM \u2192 LOW)"
        )
        rejected.append(extra)

    closest_rejected = sorted(
        (c for c in candidates if c.signal_level == SIGNAL_REJECTED),
        key=lambda c: c.ranking_score,
        reverse=True,
    )[:MAX_TOTAL_SIGNALS]

    return ValueSelectionResult(top_signals=top, rejected=rejected, closest_rejected=closest_rejected)
