"""
Selection logic for the ranked HIGH/MEDIUM/LOW/REJECTED value-signal
system (see ai_predictions/value_engine.py for how each ValueCandidate's
signal_level and ranking_score are computed).

Rules (all from the spec, not invented):
- candidates are already leveled by value_engine.classify_signal before
  they reach this module -- this module only buckets, caps and dedupes;
- up to MAX_SIGNALS_PER_LEVEL per level (HIGH/MEDIUM/LOW);
- ranked within each level by ranking_score (transparent, documented in
  value_engine.compute_ranking_score), never by best_price alone;
- at most one displayed signal per event, UNLESS two signals concern
  clearly different markets and BOTH pass MEDIUM or HIGH (Step 6) -- LOW
  signals never get a second slot on the same event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from ai_predictions.value_config import (
    MAX_SIGNALS_PER_LEVEL,
    SIGNAL_HIGH,
    SIGNAL_LOW,
    SIGNAL_MEDIUM,
    SIGNAL_REJECTED,
)
from ai_predictions.value_engine import ValueCandidate

_DISPLAYED_LEVELS = (SIGNAL_HIGH, SIGNAL_MEDIUM, SIGNAL_LOW)
_MULTI_MARKET_ELIGIBLE_LEVELS = (SIGNAL_HIGH, SIGNAL_MEDIUM)


@dataclass
class ValueSelectionResult:
    high: List[ValueCandidate] = field(default_factory=list)
    medium: List[ValueCandidate] = field(default_factory=list)
    low: List[ValueCandidate] = field(default_factory=list)
    rejected: List[ValueCandidate] = field(default_factory=list)

    @property
    def main(self) -> List[ValueCandidate]:
        """Backward-compatible alias for any code still expecting one flat
        "recommended" list -- HIGH first, then MEDIUM, then LOW."""
        return [*self.high, *self.medium, *self.low]

    @property
    def all_displayed(self) -> List[ValueCandidate]:
        return self.main


def _dedupe_per_event(candidates: List[ValueCandidate]) -> "tuple[List[ValueCandidate], List[ValueCandidate]]":
    """At most one signal per event, except a second signal is allowed
    when it is on a clearly different market AND both candidates are
    MEDIUM or HIGH. Ranking-score order decides which signal(s) win when
    more exist than slots for that event."""
    by_event: Dict[str, List[ValueCandidate]] = {}
    for c in candidates:
        by_event.setdefault(c.event_id, []).append(c)

    kept: List[ValueCandidate] = []
    bumped: List[ValueCandidate] = []
    for event_id, group in by_event.items():
        group = sorted(group, key=lambda c: c.ranking_score, reverse=True)
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
                    f"По этому событию уже выбран более сильный сигнал того же типа "
                    f"({best.selection}, score {best.ranking_score:.2f})"
                )
                bumped.append(extra)
    return kept, bumped


def select_value_recommendations(candidates: List[ValueCandidate]) -> ValueSelectionResult:
    result = ValueSelectionResult()
    rejected: List[ValueCandidate] = list(c for c in candidates if c.signal_level == SIGNAL_REJECTED)

    for level, bucket_name in ((SIGNAL_HIGH, "high"), (SIGNAL_MEDIUM, "medium"), (SIGNAL_LOW, "low")):
        level_candidates = [c for c in candidates if c.signal_level == level]
        kept, bumped = _dedupe_per_event(level_candidates)
        rejected.extend(bumped)

        ranked = sorted(kept, key=lambda c: c.ranking_score, reverse=True)
        top = ranked[:MAX_SIGNALS_PER_LEVEL]
        overflow = ranked[MAX_SIGNALS_PER_LEVEL:]
        for extra in overflow:
            extra.rejection_reasons.append(
                f"Лимит в {MAX_SIGNALS_PER_LEVEL} сигналов уровня {level} уже заполнен более сильными сигналами"
            )
            rejected.append(extra)

        # Cross-level de-duplication: an event already shown at a higher
        # level must not also show a second signal on the SAME market, and
        # a second signal on a genuinely DIFFERENT market only survives
        # when both the already-shown and the new signal are MEDIUM or
        # HIGH -- a LOW never gets a second slot on an event that already
        # has a stronger signal showing, even on a different market.
        already_shown_by_event: Dict[str, List[ValueCandidate]] = {}
        for c in result.main:
            already_shown_by_event.setdefault(c.event_id, []).append(c)

        final_top = []
        for c in top:
            existing = already_shown_by_event.get(c.event_id, [])
            same_market_conflict = any(e.market_type == c.market_type for e in existing)
            blocked = False
            if same_market_conflict:
                c.rejection_reasons.append(
                    "По этому событию и рынку уже показан сигнал более высокого уровня"
                )
                blocked = True
            elif existing:
                # Different market(s) already shown -- only allowed to add
                # this one if it and every already-shown signal on this
                # event are both MEDIUM or HIGH.
                both_strong = c.signal_level in _MULTI_MARKET_ELIGIBLE_LEVELS and all(
                    e.signal_level in _MULTI_MARKET_ELIGIBLE_LEVELS for e in existing
                )
                if not both_strong:
                    c.rejection_reasons.append(
                        "По этому событию уже показан сигнал на другом рынке; второй слот "
                        "разрешён только когда оба сигнала уровня MEDIUM или HIGH"
                    )
                    blocked = True
            if blocked:
                rejected.append(c)
            else:
                final_top.append(c)
        setattr(result, bucket_name, final_top)

    return ValueSelectionResult(high=result.high, medium=result.medium, low=result.low, rejected=rejected)
