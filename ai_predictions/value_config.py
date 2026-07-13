"""
Central, version-1 configuration for the ranked HIGH/MEDIUM/LOW/REJECTED
value-signal system (see ai_predictions/value_engine.py). Every threshold
that decides a signal's level lives here as a named constant -- nothing
in value_engine.py, value_selector.py or value_report.py hard-codes a
number, so tuning the model later means editing exactly this file.

All thresholds below are exactly the version-1 production numbers from
the spec. Nothing here is invented or guessed.
"""

from __future__ import annotations

MODEL_VERSION = "value-ranking-v2.0"

SIGNAL_HIGH = "HIGH"
SIGNAL_MEDIUM = "MEDIUM"
SIGNAL_LOW = "LOW"
SIGNAL_REJECTED = "REJECTED"
ALL_SIGNAL_LEVELS = (SIGNAL_HIGH, SIGNAL_MEDIUM, SIGNAL_LOW, SIGNAL_REJECTED)

SIGNAL_LABELS = {
    SIGNAL_HIGH: "🔥 HIGH",
    SIGNAL_MEDIUM: "🟡 MEDIUM",
    SIGNAL_LOW: "⚪ LOW",
    SIGNAL_REJECTED: "Отклонено",
}

# ---------------------------------------------------------------------------
# Per-level thresholds. A candidate is assigned the HIGHEST level whose
# *complete* set of conditions it satisfies (checked HIGH -> MEDIUM -> LOW
# -> REJECTED, first full match wins -- see value_engine.classify_signal).
# ---------------------------------------------------------------------------

HIGH_MIN_BOOKMAKERS = 3
HIGH_MIN_EV = 0.08
HIGH_MIN_EDGE = 0.03

MEDIUM_MIN_BOOKMAKERS = 2
MEDIUM_MIN_EV = 0.05
MEDIUM_MIN_EDGE = 0.02

LOW_MIN_BOOKMAKERS = 2
LOW_MIN_EV = 0.03
LOW_MIN_EDGE = 0.01

#: Below this price nothing is a real decimal price at all (mirrors
#: matching.normalize_price's own >1.0 rule; kept here too since it is a
#: named condition of every level in the spec).
MIN_BEST_ODDS = 1.01

# ---------------------------------------------------------------------------
# Outlier detection (Step 3 / Step 4 of the spec).
# ---------------------------------------------------------------------------

#: If the best price is more than this fraction higher than the
#: second-best real price for the same outcome, the best price is an
#: "isolated outlier" -- likely a pricing mistake, stale quote, or a
#: bookmaker not yet synced with the market, not necessarily genuine
#: value. Example: best=1.30, threshold=0.10 -> flagged if second-best
#: is below 1.30 / 1.10 = 1.1818.
OUTLIER_PRICE_GAP_THRESHOLD = 0.10

#: A bookmaker's price counts as "near the best price" (used for the
#: price-clustering diagnostic and outlier assessment) when it is within
#: this fraction of the best price.
NEAR_BEST_PRICE_PCT = 0.02

#: An outlier warning demotes the signal exactly one level:
#: HIGH -> MEDIUM -> LOW -> REJECTED. Documented in value_engine.classify_signal.
OUTLIER_DEMOTES_BY_ONE_LEVEL = True

# ---------------------------------------------------------------------------
# Output volume / ranking (Step 6).
# ---------------------------------------------------------------------------

#: Global cap across ALL levels combined (Step 8/9/10 of the
#: production-discovery spec): the report shows at most this many signals
#: total, filled HIGH first, then MEDIUM, then LOW -- never padded with
#: weaker candidates just to reach this number.
MAX_TOTAL_SIGNALS = 5

#: Ranking-score weights (see value_engine.compute_ranking_score for the
#: exact formula). All real, observable inputs -- EV, edge, bookmaker
#: count, price dispersion, outlier penalty -- nothing about the score
#: depends on the size of the quoted price alone.
RANKING_WEIGHT_EV = 60.0
RANKING_WEIGHT_EDGE = 25.0
RANKING_WEIGHT_BOOKMAKERS = 3.0
RANKING_WEIGHT_DISPERSION_PENALTY = 15.0
RANKING_OUTLIER_PENALTY = 20.0

# ---------------------------------------------------------------------------
# Analysis window -- unchanged from the existing production behaviour.
# Re-exported here only so all "the numbers that govern the model" are
# discoverable from one config surface; ai_predictions/window.py remains
# the single source of truth and is NOT duplicated logic, just referenced.
# ---------------------------------------------------------------------------

from ai_predictions.window import WINDOW_HOURS  # noqa: E402,F401
