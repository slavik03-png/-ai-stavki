"""
Candidate model for the selection engine.

`CandidatePrediction` is the single structure that flows through
scoring -> calibration -> filters -> diversification -> selector. It is
built incrementally: a caller (or a test) first supplies the "raw" fields
(everything the odds/statistics providers can give you directly), then
`selection_engine.scoring` fills in the derived fields (fair_odds,
expected_value, edge, confidence_score, ...) via `scoring.score_candidate`.

Probability convention: every probability field on this dataclass is a
float in the 0..1 range. `confidence_score` is 0..100 (matching
`tracking`/`football`). See `selection_engine/__init__.py` docstring.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ---------------------------------------------------------------------------
# Output groups (section 17)
# ---------------------------------------------------------------------------

GROUP_MAIN = "MAIN"
GROUP_RESERVE = "RESERVE"
GROUP_HIGH_RISK = "HIGH_RISK"
GROUP_AVOID = "AVOID"
GROUP_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

ALL_OUTPUT_GROUPS = frozenset({
    GROUP_MAIN, GROUP_RESERVE, GROUP_HIGH_RISK, GROUP_AVOID, GROUP_INSUFFICIENT_DATA,
})


@dataclass
class CandidatePrediction:
    # -- identity / context -------------------------------------------------
    event_id: str
    sport: str
    league: Optional[str]
    country: Optional[str]
    match_datetime: str  # ISO-8601 UTC
    home_team: str
    away_team: str

    # -- market -------------------------------------------------------------
    market_type: str
    selection: str
    line: Optional[float]
    bookmaker: str

    # -- pricing / probability -----------------------------------------------
    odds: float
    model_probability: float  # 0..1, deterministic output of the model
    bookmaker_implied_probability: Optional[float] = None  # 0..1
    fair_odds: Optional[float] = None
    expected_value: Optional[float] = None
    edge: Optional[float] = None

    # -- quality / reliability -------------------------------------------------
    confidence_score: Optional[float] = None  # 0..100
    data_completeness: Optional[float] = None  # 0..1
    sample_size: int = 0
    market_reliability: Optional[float] = None  # 0..1

    # -- historical performance (from tracking) --------------------------------
    historical_market_win_rate: Optional[float] = None  # 0..1
    historical_market_roi: Optional[float] = None  # percent
    historical_model_version_win_rate: Optional[float] = None  # 0..1
    historical_model_version_roi: Optional[float] = None  # percent

    # -- classification -------------------------------------------------------
    recommendation_group: Optional[str] = None  # MAIN | RESERVE | HIGH_RISK | AVOID | INSUFFICIENT_DATA
    model_version: str = ""
    generated_at: str = ""

    # -- explanation / risk -----------------------------------------------------
    explanation: List[str] = field(default_factory=list)
    risk_factors: List[str] = field(default_factory=list)

    # -- correlation / freshness ------------------------------------------------
    correlated_group_id: Optional[str] = None
    source_data_timestamp: Optional[str] = None

    # -- internal working state (not part of the required spec field list, but
    # needed to carry raw inputs through the pipeline without inventing data) --
    available_fields: Dict[str, bool] = field(default_factory=dict)
    price_timestamp: Optional[str] = None
    is_contradictory: bool = False
    rejection_reasons: List[str] = field(default_factory=list)
    selection_score: Optional[float] = None
    calibrated_probability: Optional[float] = None
    calibration_sample_size: int = 0
    calibration_is_preliminary: bool = True

    def __post_init__(self) -> None:
        if not self.generated_at:
            self.generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    @property
    def is_correct_score(self) -> bool:
        return self.market_type == "correct_score"

    @property
    def dedup_key(self) -> str:
        from tracking.models import dedup_key
        return dedup_key(self.event_id, self.market_type, self.selection, self.line, self.model_version)
