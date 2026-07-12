"""
API-Football provider -- STRUCTURAL TEMPLATE ONLY.

This class exists so the rest of the codebase (report renderer, and
eventually the bot) can be wired to a real data source later by changing a
single line, without any further redesign. Right now it deliberately:

- makes NO network requests (this module does not even import `requests`);
- reads NO secret (FOOTBALL_API_KEY is not required to instantiate or use
  this class yet);
- returns Stat.missing(...) for every method, clearly labelled as a
  template response.

When this provider is actually activated in a future task, each method
below should be implemented against the API-Football v3 REST API
(https://v3.football.api-sports.io) roughly as follows -- this mapping is
recorded here so the implementation work is a translation exercise, not a
design exercise:

- get_upcoming_matches  -> GET /fixtures?team={id}&next={limit}
- get_last_matches      -> GET /fixtures?team={id}&last={count}
- get_home_away_form    -> derived client-side from get_last_matches (venue
                           field in each fixture)
- get_head_to_head      -> GET /fixtures/headtohead?h2h={id1}-{id2}&last={count}
- get_goals_by_half     -> derived client-side from get_last_matches
                           (score.halftime / score.fulltime fields);
                           finer time-interval breakdown would additionally
                           need GET /fixtures/events?fixture={id} per match
- get_btts_frequency    -> derived client-side from get_last_matches
- get_clean_sheets      -> GET /teams/statistics?team={id}&league={id}&season={year}
- get_corners           -> GET /fixtures/statistics?fixture={id} per match (bounded depth)
- get_fouls             -> GET /fixtures/statistics?fixture={id} per match (bounded depth)
- get_cards             -> GET /teams/statistics (season aggregate) or
                           /fixtures/statistics per match for recent-form detail
- get_shots             -> GET /fixtures/statistics?fixture={id} per match (bounded depth)
- get_standings         -> GET /standings?league={id}&season={year}
- get_lineups           -> GET /fixtures/lineups?fixture={id}
- get_injuries          -> GET /injuries?team={id}&season={year}

Team names would need to be resolved to API-Football team IDs first via
GET /teams?search={name}, and cached to avoid repeated lookups.

Activating this provider will require the FOOTBALL_API_KEY secret and will
consume API-Football request quota -- both intentionally deferred until
explicitly requested.
"""

from __future__ import annotations

from typing import List, Optional

from football.interface import (
    AverageStat,
    CardsStat,
    CleanSheetStat,
    FootballStatisticsProvider,
    FormSplit,
    GoalsByHalf,
    InjuryEntry,
    MatchSummary,
    ShotsStat,
    Stat,
    StandingRow,
    TeamLineup,
)

_TEMPLATE_REASON = (
    "Провайдер API-Football — шаблон, ещё не подключён "
    "(нет обращений к сети, ключ не требуется)"
)


class ApiFootballProvider(FootballStatisticsProvider):
    name = "api_football"

    def __init__(self, api_key: Optional[str] = None) -> None:
        # Stored for future use only. Not read from the environment and not
        # validated here on purpose -- this provider must be instantiable
        # and safely callable with zero configuration.
        self.api_key = api_key

    def get_upcoming_matches(self, team: str, limit: int = 5) -> Stat[List[MatchSummary]]:
        return Stat.missing(_TEMPLATE_REASON)

    def get_last_matches(self, team: str, count: int = 10) -> Stat[List[MatchSummary]]:
        return Stat.missing(_TEMPLATE_REASON)

    def get_home_away_form(self, team: str, count: int = 10) -> Stat[FormSplit]:
        return Stat.missing(_TEMPLATE_REASON)

    def get_head_to_head(self, team_a: str, team_b: str, count: int = 10) -> Stat[List[MatchSummary]]:
        return Stat.missing(_TEMPLATE_REASON)

    def get_goals_by_half(self, team: str, count: int = 10) -> Stat[GoalsByHalf]:
        return Stat.missing(_TEMPLATE_REASON)

    def get_btts_frequency(self, team: str, count: int = 10) -> Stat[str]:
        return Stat.missing(_TEMPLATE_REASON)

    def get_clean_sheets(self, team: str, count: int = 10) -> Stat[CleanSheetStat]:
        return Stat.missing(_TEMPLATE_REASON)

    def get_corners(self, team: str, count: int = 10) -> Stat[AverageStat]:
        return Stat.missing(_TEMPLATE_REASON)

    def get_fouls(self, team: str, count: int = 10) -> Stat[AverageStat]:
        return Stat.missing(_TEMPLATE_REASON)

    def get_cards(self, team: str, count: int = 10) -> Stat[CardsStat]:
        return Stat.missing(_TEMPLATE_REASON)

    def get_shots(self, team: str, count: int = 10) -> Stat[ShotsStat]:
        return Stat.missing(_TEMPLATE_REASON)

    def get_standings(self, league: str) -> Stat[List[StandingRow]]:
        return Stat.missing(_TEMPLATE_REASON)

    def get_lineups(self, home_team: str, away_team: str) -> Stat[List[TeamLineup]]:
        return Stat.missing(_TEMPLATE_REASON)

    def get_injuries(self, team: str) -> Stat[List[InjuryEntry]]:
        return Stat.missing(_TEMPLATE_REASON)
