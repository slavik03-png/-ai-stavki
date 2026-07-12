"""
Selection logic for the cross-bookmaker value-detection strategy (see
ai_predictions/value_engine.py for how each ValueCandidate is computed).

Rules (all from the user-facing spec, not invented):
- at most one main recommendation per event,
- at most MAX_MAIN_RECOMMENDATIONS total,
- only candidates that already passed value_engine's own thresholds
  (>=3 bookmakers, real leave-one-out edge, positive expected value) are
  eligible,
- ranked by edge (the strongest, most real divergence first),
- never padded with weak candidates just to reach the cap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from ai_predictions.value_engine import MAX_MAIN_RECOMMENDATIONS, ValueCandidate


@dataclass
class ValueSelectionResult:
    main: List[ValueCandidate] = field(default_factory=list)
    rejected: List[ValueCandidate] = field(default_factory=list)


def select_value_recommendations(candidates: List[ValueCandidate]) -> ValueSelectionResult:
    passed = [c for c in candidates if c.passed]
    rejected = [c for c in candidates if not c.passed]

    # At most one recommendation per event: keep only the strongest edge.
    best_per_event: Dict[str, ValueCandidate] = {}
    for candidate in passed:
        current = best_per_event.get(candidate.event_id)
        if current is None or candidate.edge > current.edge:
            if current is not None:
                current.rejection_reasons.append(
                    f"По этому событию уже выбран более сильный сигнал ({current.selection}, "
                    f"расхождение {current.edge:.3f})"
                )
                rejected.append(current)
            best_per_event[candidate.event_id] = candidate
        else:
            candidate.rejection_reasons.append(
                f"По этому событию уже выбран более сильный сигнал ({current.selection}, "
                f"расхождение {current.edge:.3f})"
            )
            rejected.append(candidate)

    ranked = sorted(best_per_event.values(), key=lambda c: c.edge, reverse=True)
    main = ranked[:MAX_MAIN_RECOMMENDATIONS]
    for extra in ranked[MAX_MAIN_RECOMMENDATIONS:]:
        extra.rejection_reasons.append(
            f"Лимит в {MAX_MAIN_RECOMMENDATIONS} рекомендаций уже заполнен более сильными сигналами"
        )
        rejected.append(extra)

    return ValueSelectionResult(main=main, rejected=rejected)
