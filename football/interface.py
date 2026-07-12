"""
Provider-agnostic football statistics contract.

Every statistics source (mock data, API-Football, or any future provider)
implements `FootballStatisticsProvider`. Callers (report renderers, and
eventually the bot) code against this interface only -- they never import a
concrete provider directly, so the data source can be swapped by changing
one line of wiring code.

Core rule: never invent data. Every method returns a `Stat`, which is either:
- `Stat.ok(value)`       -- real data was retrieved
- `Stat.missing(reason)` -- data could not be retrieved; `reason` explains why

`reason` should be a short, human-readable (Russian is fine) explanation
such as "провайдер не поддерживает эту статистику" or "нет завершённых
матчей за период".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Generic, List, Optional, TypeVar

T = TypeVar("T")


@dataclass
class Stat(Generic[T]):
    available: bool
    value: Optional[T] = None
    reason: Optional[str] = None

    @staticmethod
    def ok(value: T) -> "Stat[T]":
        return Stat(available=True, value=value, reason=None)

    @staticmethod
    def missing(reason: str) -> "Stat[T]":
        return Stat(available=False, value=None, reason=reason)


# ---------------------------------------------------------------------------
# Data shapes returned inside Stat.value
# ---------------------------------------------------------------------------

@dataclass
class MatchSummary:
    date: str
    home_team: str
    away_team: str
    home_goals: Optional[int]
    away_goals: Optional[int]
    ht_home_goals: Optional[int] = None
    ht_away_goals: Optional[int] = None
    competition: Optional[str] = None
    venue: Optional[str] = None
    status: Optional[str] = None


@dataclass
class FormSplit:
    overall: str  # e.g. "WWDLW", most recent last
    home: str
    away: str
    matches_counted: int


@dataclass
class GoalsByHalf:
    first_half_scored_avg: Optional[float]
    first_half_conceded_avg: Optional[float]
    second_half_scored_avg: Optional[float]
    second_half_conceded_avg: Optional[float]
    matches_counted: int
    # Finer time-interval breakdown (e.g. "0-15", "76-90"). Most providers
    # only expose half-based splits, so this is optional and may be missing
    # even when the half-based fields above are available.
    intervals: Optional[Dict[str, Any]] = None
    intervals_reason: Optional[str] = None


@dataclass
class CleanSheetStat:
    clean_sheets: int
    failed_to_score: int
    matches_counted: int


@dataclass
class AverageStat:
    average: float
    matches_counted: int


@dataclass
class CardsStat:
    avg_yellow: float
    avg_red: float
    matches_counted: int


@dataclass
class ShotsStat:
    avg_total: float
    avg_on_target: float
    matches_counted: int


@dataclass
class StandingRow:
    team: str
    rank: int
    points: int
    played: int


@dataclass
class LineupPlayer:
    name: str
    position: Optional[str] = None


@dataclass
class TeamLineup:
    team: str
    formation: Optional[str]
    starters: List[LineupPlayer]


@dataclass
class InjuryEntry:
    player: str
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

class FootballStatisticsProvider(ABC):
    """
    Abstract base class every football statistics source must implement.

    Teams are identified by plain display names (str) to keep the interface
    provider-agnostic -- each concrete provider resolves names to whatever
    internal ID scheme it needs.
    """

    #: Short machine-readable identifier, e.g. "mock", "api_football".
    name: str = "base"

    @abstractmethod
    def get_upcoming_matches(self, team: str, limit: int = 5) -> Stat[List[MatchSummary]]:
        """Upcoming scheduled fixtures for a team."""

    @abstractmethod
    def get_last_matches(self, team: str, count: int = 10) -> Stat[List[MatchSummary]]:
        """Most recent finished matches for a team."""

    @abstractmethod
    def get_home_away_form(self, team: str, count: int = 10) -> Stat[FormSplit]:
        """Overall, home-only, and away-only W/D/L form strings."""

    @abstractmethod
    def get_head_to_head(self, team_a: str, team_b: str, count: int = 10) -> Stat[List[MatchSummary]]:
        """Historical meetings between two teams."""

    @abstractmethod
    def get_goals_by_half(self, team: str, count: int = 10) -> Stat[GoalsByHalf]:
        """Average goals scored/conceded per half (and time intervals, if the provider supports them)."""

    @abstractmethod
    def get_btts_frequency(self, team: str, count: int = 10) -> Stat[str]:
        """Both-teams-to-score frequency as an 'x/y' string."""

    @abstractmethod
    def get_clean_sheets(self, team: str, count: int = 10) -> Stat[CleanSheetStat]:
        """Clean sheet and failed-to-score counts."""

    @abstractmethod
    def get_corners(self, team: str, count: int = 10) -> Stat[AverageStat]:
        """Average corners per match."""

    @abstractmethod
    def get_fouls(self, team: str, count: int = 10) -> Stat[AverageStat]:
        """Average fouls per match."""

    @abstractmethod
    def get_cards(self, team: str, count: int = 10) -> Stat[CardsStat]:
        """Average yellow/red cards per match."""

    @abstractmethod
    def get_shots(self, team: str, count: int = 10) -> Stat[ShotsStat]:
        """Average total shots and shots on target per match."""

    @abstractmethod
    def get_standings(self, league: str) -> Stat[List[StandingRow]]:
        """Current league table."""

    @abstractmethod
    def get_lineups(self, home_team: str, away_team: str) -> Stat[List[TeamLineup]]:
        """Starting lineups for an upcoming/recent match, if published."""

    @abstractmethod
    def get_injuries(self, team: str) -> Stat[List[InjuryEntry]]:
        """Injured or otherwise unavailable players."""
