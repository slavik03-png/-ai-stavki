"""
Explicit versioning for the selection engine.

Every stored prediction must carry the exact combination of versions that
produced it, so historical results always stay associated with the logic
that generated them. When scoring formulas or weights change, bump
AI_STAVKI_MODEL_VERSION (and, if only the weight numbers changed without a
formula change, at least SCORING_FORMULA_VERSION) -- never rewrite old
predictions to look like they came from a newer model.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Overall selection-engine model version. Increment whenever the scoring
#: formula, filter thresholds, or ranking logic changes in a way that could
#: alter which candidates get selected.
AI_STAVKI_MODEL_VERSION = "selection-v1.0"

#: Version of the SelectionConfig defaults (thresholds/weights). Bump when
#: the *values* change even if the formulas/code do not.
CONFIG_VERSION = "config-v1.0"

#: Version of the scoring formulas themselves (confidence score, selection
#: score, edge/EV calculations). Bump when the formula shape changes.
SCORING_FORMULA_VERSION = "scoring-v1.0"

#: Version of the upstream data provider integration in use. "mock" until a
#: real football statistics API is connected and approved.
PROVIDER_VERSION = "mock-v1.0"


@dataclass(frozen=True)
class VersionInfo:
    model_version: str = AI_STAVKI_MODEL_VERSION
    config_version: str = CONFIG_VERSION
    scoring_formula_version: str = SCORING_FORMULA_VERSION
    provider_version: str = PROVIDER_VERSION


def current_versions() -> VersionInfo:
    return VersionInfo()
