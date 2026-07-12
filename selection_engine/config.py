"""
Central configuration for the selection engine.

Nothing here reads secrets/environment variables -- these are ordinary
scoring/business parameters, not credentials. All thresholds and weights
are grouped in one dataclass so they are never scattered through the code,
and so they can be changed later based on verified backtesting without
touching scoring/filter/selector logic.

These are *initial conservative defaults*, not permanent truths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List

from selection_engine.versioning import AI_STAVKI_MODEL_VERSION

# ---------------------------------------------------------------------------
# Market catalogue
# ---------------------------------------------------------------------------

#: Every market type the engine is capable of evaluating, mirroring
#: tracking.settlement.SUPPORTED_MARKET_TYPES so a candidate is never
#: produced for a market the tracking system cannot later settle.
MARKET_1X2 = "1x2"
MARKET_DOUBLE_CHANCE = "double_chance"
MARKET_DRAW_NO_BET = "draw_no_bet"
MARKET_BTTS = "btts"
MARKET_TOTAL_GOALS = "total_goals"
MARKET_ASIAN_TOTAL = "asian_total"
MARKET_TEAM_TOTAL = "team_total"
MARKET_FIRST_HALF_TOTAL = "first_half_total"
MARKET_SECOND_HALF_TOTAL = "second_half_total"
MARKET_GOAL_BOTH_HALVES = "goal_both_halves"
MARKET_CORRECT_SCORE = "correct_score"
MARKET_CORNERS_TOTAL = "corners_total"
MARKET_CARDS_TOTAL = "cards_total"
MARKET_FOULS_TOTAL = "fouls_total"
MARKET_SHOTS_TOTAL = "shots_total"

ALL_MARKET_TYPES: FrozenSet[str] = frozenset({
    MARKET_1X2, MARKET_DOUBLE_CHANCE, MARKET_DRAW_NO_BET, MARKET_BTTS,
    MARKET_TOTAL_GOALS, MARKET_ASIAN_TOTAL, MARKET_TEAM_TOTAL,
    MARKET_FIRST_HALF_TOTAL, MARKET_SECOND_HALF_TOTAL, MARKET_GOAL_BOTH_HALVES,
    MARKET_CORRECT_SCORE, MARKET_CORNERS_TOTAL, MARKET_CARDS_TOTAL,
    MARKET_FOULS_TOTAL, MARKET_SHOTS_TOTAL,
})

#: Markets always treated as high risk regardless of confidence/EV, and
#: never eligible for MAIN unless explicitly re-enabled in configuration.
DEFAULT_HIGH_RISK_MARKETS: FrozenSet[str] = frozenset({MARKET_CORRECT_SCORE})

#: Required vs optional data inputs per market, used to compute
#: data_completeness. Field names are descriptive labels, not code
#: identifiers -- callers supply a Dict[str, bool] of which of these were
#: actually retrieved for a given candidate.
MARKET_DATA_REQUIREMENTS: Dict[str, Dict[str, List[str]]] = {
    MARKET_1X2: {
        "required": ["home_form", "away_form", "sample_size"],
        "optional": ["h2h", "league_position", "injuries", "lineups"],
    },
    MARKET_DOUBLE_CHANCE: {
        "required": ["home_form", "away_form", "sample_size"],
        "optional": ["h2h", "league_position"],
    },
    MARKET_DRAW_NO_BET: {
        "required": ["home_form", "away_form", "sample_size"],
        "optional": ["h2h", "league_position"],
    },
    MARKET_BTTS: {
        "required": ["btts_frequency_home", "btts_frequency_away", "sample_size"],
        "optional": ["clean_sheets_home", "clean_sheets_away", "goals_scored_conceded"],
    },
    MARKET_TOTAL_GOALS: {
        "required": ["goals_scored_conceded", "sample_size", "current_price"],
        "optional": ["h2h", "league_position"],
    },
    MARKET_ASIAN_TOTAL: {
        "required": ["goals_scored_conceded", "sample_size", "current_price"],
        "optional": ["h2h"],
    },
    MARKET_TEAM_TOTAL: {
        "required": ["goals_scored_conceded", "sample_size", "current_price"],
        "optional": ["home_away_form", "clean_sheets"],
    },
    MARKET_FIRST_HALF_TOTAL: {
        "required": ["first_half_performance", "sample_size"],
        "optional": ["h2h"],
    },
    MARKET_SECOND_HALF_TOTAL: {
        "required": ["second_half_performance", "sample_size"],
        "optional": ["h2h"],
    },
    MARKET_GOAL_BOTH_HALVES: {
        "required": ["first_half_performance", "second_half_performance", "sample_size"],
        "optional": [],
    },
    MARKET_CORRECT_SCORE: {
        "required": ["goals_scored_conceded", "recent_matches", "sample_size"],
        "optional": ["h2h", "lineups"],
    },
    MARKET_CORNERS_TOTAL: {
        "required": ["corners", "sample_size", "current_price"],
        "optional": [],
    },
    MARKET_CARDS_TOTAL: {
        "required": ["cards", "sample_size", "current_price"],
        "optional": ["lineups"],
    },
    MARKET_FOULS_TOTAL: {
        "required": ["fouls", "sample_size", "current_price"],
        "optional": [],
    },
    MARKET_SHOTS_TOTAL: {
        "required": ["shots", "sample_size", "current_price"],
        "optional": [],
    },
}

# ---------------------------------------------------------------------------
# Correlation groups (used by diversification.py)
# ---------------------------------------------------------------------------

#: Each candidate's correlation family is derived from (market_type,
#: selection-shape). Markets in the same family are considered strongly
#: correlated and only the strongest one per event is kept.
CORRELATED_FAMILIES: Dict[str, str] = {
    MARKET_1X2: "match_result",
    MARKET_DOUBLE_CHANCE: "match_result",
    MARKET_DRAW_NO_BET: "match_result",
    MARKET_BTTS: "goals_pattern",
    MARKET_GOAL_BOTH_HALVES: "goals_pattern",
    MARKET_TOTAL_GOALS: "total_goals",
    MARKET_ASIAN_TOTAL: "total_goals",
    MARKET_TEAM_TOTAL: "team_total",
    MARKET_FIRST_HALF_TOTAL: "half_total",
    MARKET_SECOND_HALF_TOTAL: "half_total",
    MARKET_CORRECT_SCORE: "correct_score",
    MARKET_CORNERS_TOTAL: "corners",
    MARKET_CARDS_TOTAL: "cards",
    MARKET_FOULS_TOTAL: "fouls",
    MARKET_SHOTS_TOTAL: "shots",
}


def correlation_family(market_type: str) -> str:
    return CORRELATED_FAMILIES.get(market_type, market_type)


# ---------------------------------------------------------------------------
# Sample-size reliability bands
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SampleBand:
    max_matches: int  # exclusive upper bound; None-like via math.inf for the top band
    factor: float
    label: str


#: Deterministic sample-reliability bands. `max_matches` is the exclusive
#: upper bound of relevant-match count for that band (the last band has no
#: upper bound). These boundaries are configurable, not proof of
#: reliability by themselves.
DEFAULT_SAMPLE_BANDS: List[SampleBand] = [
    SampleBand(max_matches=5, factor=0.15, label="очень слабая выборка"),
    SampleBand(max_matches=10, factor=0.45, label="слабая выборка"),
    SampleBand(max_matches=20, factor=0.75, label="умеренная выборка"),
    SampleBand(max_matches=10 ** 9, factor=1.0, label="достаточная выборка"),
]

# ---------------------------------------------------------------------------
# Confidence score weights (must sum in a documented, bounded way -- see
# scoring.compute_confidence_score for the exact formula)
# ---------------------------------------------------------------------------

@dataclass
class ConfidenceWeights:
    probability_weight: float = 30.0
    value_weight: float = 20.0
    data_completeness_weight: float = 15.0
    sample_reliability_weight: float = 10.0
    market_reliability_weight: float = 10.0
    historical_calibration_weight: float = 10.0
    historical_market_weight: float = 5.0
    missing_data_penalty_per_gap: float = 6.0
    contradiction_penalty: float = 10.0
    stale_data_penalty: float = 15.0


# ---------------------------------------------------------------------------
# Ranking (selection score) weights
# ---------------------------------------------------------------------------

@dataclass
class RankingWeights:
    confidence_weight: float = 0.35
    value_weight: float = 0.25
    historical_reliability_weight: float = 0.15
    data_quality_weight: float = 0.15
    odds_sanity_weight: float = 0.10
    correlation_penalty: float = 12.0
    risk_penalty: float = 8.0


# ---------------------------------------------------------------------------
# Master configuration
# ---------------------------------------------------------------------------

@dataclass
class SelectionConfig:
    # Output limits (section 14/17)
    max_main_recommendations: int = 5
    max_reserve_recommendations: int = 3
    max_main_per_event: int = 1
    max_high_risk: int = 3
    max_avoid_shown: int = 5
    max_main_per_league: int = 2
    max_main_per_market_family: int = 2

    # Minimum filters (section 12) -- conservative initial defaults
    min_confidence_score: float = 75.0
    preferred_confidence_score: float = 80.0
    min_data_completeness: float = 0.80
    min_edge: float = 0.04
    min_expected_value: float = 0.03
    min_decimal_odds: float = 1.25
    max_decimal_odds_main: float = 3.50
    decisive_sample_warning_threshold: int = 20

    # Data freshness (section "freshness of data")
    max_price_age_minutes: float = 30.0
    max_stat_age_hours: float = 48.0

    # Risk-group rules
    high_risk_markets: FrozenSet[str] = field(default_factory=lambda: DEFAULT_HIGH_RISK_MARKETS)
    allow_high_risk_as_main: bool = False

    # Allowed / disabled markets. Empty allowed set means "all ALL_MARKET_TYPES
    # are allowed" -- disabled_markets is always subtracted afterwards.
    allowed_markets: FrozenSet[str] = field(default_factory=lambda: ALL_MARKET_TYPES)
    disabled_markets: FrozenSet[str] = field(default_factory=frozenset)

    # Sample bands
    sample_bands: List[SampleBand] = field(default_factory=lambda: list(DEFAULT_SAMPLE_BANDS))

    # Weights
    confidence_weights: ConfidenceWeights = field(default_factory=ConfidenceWeights)
    ranking_weights: RankingWeights = field(default_factory=RankingWeights)

    # Calibration settings
    calibration_min_sample: int = 20
    calibration_buckets: List[float] = field(
        default_factory=lambda: [50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    )
    #: Maximum probability adjustment (absolute, 0..1 scale) calibration is
    #: allowed to apply, even with a large sample -- keeps calibration
    #: conservative rather than letting it swing wildly.
    calibration_max_adjustment: float = 0.10

    # Model version tag stored on every candidate/prediction produced here.
    model_version: str = AI_STAVKI_MODEL_VERSION

    def effective_allowed_markets(self) -> FrozenSet[str]:
        return frozenset(self.allowed_markets) - frozenset(self.disabled_markets)


def default_config() -> SelectionConfig:
    return SelectionConfig()
