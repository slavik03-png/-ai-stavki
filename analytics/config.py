"""
Constants for the AI Betting Analytics module. Kept independent of
ai_predictions/value_config.py on purpose (spec requirement: an
independent module) -- a couple of small display labels are duplicated
here rather than imported, so this package never depends on ai_predictions.
"""

from __future__ import annotations

import os

#: Separate, permanent SQLite database -- never the same file as
#: tracking/storage.py's data/ai_stavki.db or ai_predictions'
#: data/api_football_cache.db.
ANALYTICS_DB_PATH = os.path.join("data", "analytics.db")

#: Default flat stake per prediction, in abstract currency units. ROI% is
#: scale-invariant under flat staking, so changing this only rescales the
#: displayed profit figures, never the win rate or ROI percentage.
DEFAULT_STAKE = 100.0

#: How often the background result checker wakes up to look for finished
#: fixtures among pending predictions.
RESULT_CHECK_INTERVAL_MINUTES = 30

#: A match is only even considered for a result check once this many
#: hours have passed since its scheduled kickoff (regular time + stoppage
#: + a safety margin before the final status can possibly be "FT").
RESULT_CHECK_MIN_HOURS_AFTER_KICKOFF = 2.5

#: API-Football statuses meaning the match result is final and will never
#: change again (safe to cache permanently and to settle from).
FINISHED_STATUSES = {"FT", "AET", "PEN"}

#: API-Football statuses meaning the match will not produce a result at
#: all under its original schedule (settled as VOID via settle_prediction's
#: postponed/cancelled handling).
VOID_STATUSES = {"PST", "CANC", "ABD", "WO"}

#: This project's own composite market keys (see
#: ai_predictions/football_predictions.py and
#: ai_predictions/value_config.BET_MARKET_LABELS_RU) translated into
#: tracking.settlement's (market_type, selection, line) vocabulary. Every
#: key ever produced by build_candidates_for_fixture / historical-baseline
#: candidates must have an entry here, or settlement of that market will
#: correctly surface as an explicit error rather than being silently
#: skipped.
MARKET_KEY_MAP = {
    "home_win": ("1x2", "home", None),
    "draw": ("1x2", "draw", None),
    "away_win": ("1x2", "away", None),
    "double_chance_1x": ("double_chance", "1x", None),
    "double_chance_x2": ("double_chance", "x2", None),
    "over_1_5": ("total_goals", "over", 1.5),
    "over_2_5": ("total_goals", "over", 2.5),
    "under_3_5": ("total_goals", "under", 3.5),
    "btts_yes": ("btts", "yes", None),
    "btts_no": ("btts", "no", None),
}

#: Display labels for reports (independent copy of the same Russian
#: labels used elsewhere in the project, so this module never imports
#: ai_predictions/value_config.py).
MARKET_LABELS_RU = {
    "home_win": "Победа хозяев",
    "draw": "Ничья",
    "away_win": "Победа гостей",
    "double_chance_1x": "Двойной шанс 1X",
    "double_chance_x2": "Двойной шанс X2",
    "over_1_5": "Тотал больше 1.5",
    "over_2_5": "Тотал больше 2.5",
    "under_3_5": "Тотал меньше 3.5",
    "btts_yes": "Обе забьют — да",
    "btts_no": "Обе забьют — нет",
}

#: Settlement outcomes considered a full/partial win for reporting.
WIN_STATUSES = {"won", "half_won"}
LOSS_STATUSES = {"lost", "half_lost"}
VOID_RESULT_STATUSES = {"returned", "cancelled", "postponed"}
