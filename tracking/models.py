"""
Data model for the prediction-tracking package.

Two dataclasses:
- `Prediction`  -- one recommendation the bot issued (or could issue), plus
  its eventual settlement outcome.
- `EventResult` -- the real-world outcome of the underlying event, as
  returned by a result provider (`tracking.result_checker`). Only fields
  that were actually retrieved are populated; everything else stays `None`
  and settlement treats it as missing, never as zero.

No network calls, no Telegram, no bot.py dependency here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------

STATUS_PENDING = "pending"
STATUS_WON = "won"
STATUS_LOST = "lost"
STATUS_RETURNED = "returned"
STATUS_HALF_WON = "half_won"
STATUS_HALF_LOST = "half_lost"
STATUS_CANCELLED = "cancelled"
STATUS_POSTPONED = "postponed"
STATUS_VOID = "void"
STATUS_UNRESOLVED = "unresolved"

ALL_STATUSES = {
    STATUS_PENDING, STATUS_WON, STATUS_LOST, STATUS_RETURNED,
    STATUS_HALF_WON, STATUS_HALF_LOST, STATUS_CANCELLED, STATUS_POSTPONED,
    STATUS_VOID, STATUS_UNRESOLVED,
}

#: Statuses that represent a final, no-longer-pending outcome of any kind
#: (used to decide whether a prediction is still open).
FINAL_STATUSES = ALL_STATUSES - {STATUS_PENDING}

#: Statuses counted as "settled" for statistics purposes -- a real grading
#: outcome exists. Postponed/unresolved events are final in the sense that
#: nothing more will happen to them automatically, but they carry no
#: win/loss information, so they are excluded here and reported separately.
GRADED_STATUSES = {
    STATUS_WON, STATUS_LOST, STATUS_RETURNED, STATUS_HALF_WON,
    STATUS_HALF_LOST, STATUS_CANCELLED, STATUS_VOID,
}

#: Statuses that count as a "decisive" bet for win-rate purposes -- i.e.
#: excludes pushes/voids which never had a chance to win or lose outright.
DECISIVE_STATUSES = {STATUS_WON, STATUS_LOST, STATUS_HALF_WON, STATUS_HALF_LOST}

RECOMMENDATION_GROUPS = {"main", "alternative", "high_risk", "avoid"}

#: Ranked value-signal levels (ai_predictions/value_config.py is the
#: single source of truth for the level names; duplicated here as plain
#: strings, not an import, so tracking/ has no dependency on
#: ai_predictions/). None means "not produced by the ranked signal
#: system" -- e.g. rows from the older statistics-based pipeline.
SIGNAL_LEVELS = {"HIGH", "MEDIUM", "LOW", "REJECTED"}


def new_prediction_id() -> str:
    return str(uuid.uuid4())


def dedup_key(event_id: str, market_type: str, selection: str,
              line: Optional[float], model_version: str) -> str:
    """Stable uniqueness key: same bet on the same event/model must never be
    stored twice, even if issued again by mistake."""
    line_part = "none" if line is None else f"{float(line):.2f}"
    return f"{event_id}|{market_type}|{selection}|{line_part}|{model_version}"


@dataclass
class Prediction:
    sport: str
    country: Optional[str]
    league: Optional[str]
    event_id: str
    event_start_time: str  # ISO-8601 UTC
    home_team: str
    away_team: str
    market_type: str
    market_name: str  # Russian display name
    selection: str
    bookmaker_odds: float
    model_probability: float
    confidence_score: float
    confidence_level: str
    recommendation_group: str  # main | alternative | high_risk | avoid
    explanation: str
    data_provider: str
    model_version: str

    line: Optional[float] = None
    prediction_id: str = field(default_factory=new_prediction_id)
    created_at: Optional[str] = None  # set by storage on insert (UTC ISO-8601)

    status: str = STATUS_PENDING
    final_score: Optional[str] = None
    first_half_score: Optional[str] = None
    settled_at: Optional[str] = None
    settlement_explanation: Optional[str] = None

    # -- ranked HIGH/MEDIUM/LOW/REJECTED signal system (optional, backward
    #    compatible: rows from the older statistics-based pipeline simply
    #    leave these at their defaults). --
    signal_level: Optional[str] = None
    ranking_score: Optional[float] = None
    outlier_warning: bool = False
    rejection_reason: Optional[str] = None

    # -- API-Football statistics enrichment (optional, backward compatible:
    #    rows saved before this feature existed simply leave these at
    #    their defaults). --
    statistics_source: Optional[str] = None
    statistics_cached: bool = False
    statistics_completeness: Optional[float] = None
    statistics_score: Optional[float] = None
    final_combined_score: Optional[float] = None

    # -- fixture-discovery-first pipeline (optional, backward compatible:
    #    rows saved before this feature existed simply leave these at
    #    their defaults). --
    fixture_id: Optional[int] = None
    matching_confidence: Optional[float] = None
    sample_size_category: Optional[str] = None
    market_probability: Optional[float] = None
    statistics_probability: Optional[float] = None

    def __post_init__(self) -> None:
        if self.recommendation_group not in RECOMMENDATION_GROUPS:
            raise ValueError(
                f"invalid recommendation_group {self.recommendation_group!r}, "
                f"must be one of {sorted(RECOMMENDATION_GROUPS)}"
            )
        if self.status not in ALL_STATUSES:
            raise ValueError(f"invalid status {self.status!r}")
        if self.signal_level is not None and self.signal_level not in SIGNAL_LEVELS:
            raise ValueError(
                f"invalid signal_level {self.signal_level!r}, must be one of {sorted(SIGNAL_LEVELS)} or None"
            )

    @property
    def dedup_key(self) -> str:
        return dedup_key(self.event_id, self.market_type, self.selection,
                          self.line, self.model_version)


@dataclass
class EventResult:
    """Real-world outcome of one event. Any field left as None means that
    piece of data was not retrieved -- settlement must never guess it."""

    event_id: str
    status: str = "unknown"  # finished | postponed | cancelled | unknown

    home_goals: Optional[int] = None
    away_goals: Optional[int] = None
    ht_home_goals: Optional[int] = None
    ht_away_goals: Optional[int] = None

    home_corners: Optional[int] = None
    away_corners: Optional[int] = None
    home_cards: Optional[int] = None  # yellow + red combined
    away_cards: Optional[int] = None
    home_fouls: Optional[int] = None
    away_fouls: Optional[int] = None
    home_shots: Optional[int] = None
    away_shots: Optional[int] = None

    retrieved_at: Optional[str] = None

    @property
    def final_score(self) -> Optional[str]:
        if self.home_goals is None or self.away_goals is None:
            return None
        return f"{self.home_goals}:{self.away_goals}"

    @property
    def first_half_score(self) -> Optional[str]:
        if self.ht_home_goals is None or self.ht_away_goals is None:
            return None
        return f"{self.ht_home_goals}:{self.ht_away_goals}"
