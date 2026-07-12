"""
Mock football statistics provider.

Returns sample data only -- for exercising the provider architecture and
the report renderer end-to-end without any external API. Never used for
real analysis; every value here is fabricated on purpose and clearly
labelled as sample data in this file.

Also demonstrates the missing/unavailable path: a couple of fields are
deliberately returned as Stat.missing(...) for "Mock Away FC" so tests can
confirm the report renderer handles missing data instead of inventing it.
"""

from __future__ import annotations

from typing import List

from football.interface import (
    AverageStat,
    CardsStat,
    CleanSheetStat,
    FootballStatisticsProvider,
    FormSplit,
    GoalsByHalf,
    InjuryEntry,
    LineupPlayer,
    MatchSummary,
    ShotsStat,
    Stat,
    StandingRow,
    TeamLineup,
)

# Sample-only "known teams" so the mock behaves consistently across calls.
_KNOWN_TEAMS = {"Mock Home FC", "Mock Away FC"}


class MockFootballProvider(FootballStatisticsProvider):
    name = "mock"

    def _check_known(self, team: str) -> Stat:
        if team not in _KNOWN_TEAMS:
            return Stat.missing(f"Мок-провайдер не содержит данных для команды «{team}»")
        return None  # type: ignore[return-value]

    def get_upcoming_matches(self, team: str, limit: int = 5) -> Stat[List[MatchSummary]]:
        unknown = self._check_known(team)
        if unknown:
            return unknown
        return Stat.ok([
            MatchSummary(
                date="2026-08-01T18:00:00+00:00",
                home_team="Mock Home FC",
                away_team="Mock Away FC",
                home_goals=None,
                away_goals=None,
                competition="Mock League",
                venue="Mock Arena",
                status="NS",
            )
        ][:limit])

    def get_last_matches(self, team: str, count: int = 10) -> Stat[List[MatchSummary]]:
        unknown = self._check_known(team)
        if unknown:
            return unknown
        sample = [
            MatchSummary("2026-06-01", "Mock Home FC", "Mock Rival A", 2, 1, 1, 0, "Mock League", status="FT"),
            MatchSummary("2026-06-08", "Mock Rival B", "Mock Home FC", 0, 0, 0, 0, "Mock League", status="FT"),
            MatchSummary("2026-06-15", "Mock Home FC", "Mock Rival C", 3, 0, 2, 0, "Mock League", status="FT"),
        ] if team == "Mock Home FC" else [
            MatchSummary("2026-06-02", "Mock Away FC", "Mock Rival D", 1, 1, 0, 1, "Mock League", status="FT"),
            MatchSummary("2026-06-09", "Mock Rival E", "Mock Away FC", 2, 2, 1, 1, "Mock League", status="FT"),
        ]
        return Stat.ok(sample[:count])

    def get_home_away_form(self, team: str, count: int = 10) -> Stat[FormSplit]:
        unknown = self._check_known(team)
        if unknown:
            return unknown
        if team == "Mock Home FC":
            return Stat.ok(FormSplit(overall="WDW", home="WW", away="D", matches_counted=3))
        return Stat.ok(FormSplit(overall="DD", home="D", away="D", matches_counted=2))

    def get_head_to_head(self, team_a: str, team_b: str, count: int = 10) -> Stat[List[MatchSummary]]:
        if {team_a, team_b} != _KNOWN_TEAMS:
            return Stat.missing("Мок-провайдер не содержит личных встреч для этой пары команд")
        return Stat.ok([
            MatchSummary("2025-12-01", "Mock Home FC", "Mock Away FC", 1, 1, 0, 1, "Mock League", status="FT"),
        ][:count])

    def get_goals_by_half(self, team: str, count: int = 10) -> Stat[GoalsByHalf]:
        unknown = self._check_known(team)
        if unknown:
            return unknown
        if team == "Mock Home FC":
            return Stat.ok(GoalsByHalf(
                first_half_scored_avg=0.67, first_half_conceded_avg=0.0,
                second_half_scored_avg=1.0, second_half_conceded_avg=0.33,
                matches_counted=3,
                intervals=None,
                intervals_reason="Мок-провайдер не разбивает голы по 15-минутным интервалам",
            ))
        return Stat.ok(GoalsByHalf(
            first_half_scored_avg=0.5, first_half_conceded_avg=1.0,
            second_half_scored_avg=1.0, second_half_conceded_avg=0.5,
            matches_counted=2,
            intervals=None,
            intervals_reason="Мок-провайдер не разбивает голы по 15-минутным интервалам",
        ))

    def get_btts_frequency(self, team: str, count: int = 10) -> Stat[str]:
        unknown = self._check_known(team)
        if unknown:
            return unknown
        return Stat.ok("1/3" if team == "Mock Home FC" else "2/2")

    def get_clean_sheets(self, team: str, count: int = 10) -> Stat[CleanSheetStat]:
        unknown = self._check_known(team)
        if unknown:
            return unknown
        if team == "Mock Home FC":
            return Stat.ok(CleanSheetStat(clean_sheets=2, failed_to_score=1, matches_counted=3))
        return Stat.ok(CleanSheetStat(clean_sheets=0, failed_to_score=0, matches_counted=2))

    def get_corners(self, team: str, count: int = 10) -> Stat[AverageStat]:
        if team == "Mock Home FC":
            return Stat.ok(AverageStat(average=6.0, matches_counted=3))
        # Deliberately unavailable to test the missing-data path end to end.
        return Stat.missing("Детальная статистика угловых недоступна для этих матчей")

    def get_fouls(self, team: str, count: int = 10) -> Stat[AverageStat]:
        if team == "Mock Home FC":
            return Stat.ok(AverageStat(average=8.0, matches_counted=3))
        return Stat.missing("Детальная статистика фолов недоступна для этих матчей")

    def get_cards(self, team: str, count: int = 10) -> Stat[CardsStat]:
        if team == "Mock Home FC":
            return Stat.ok(CardsStat(avg_yellow=1.0, avg_red=0.0, matches_counted=3))
        return Stat.missing("Детальная статистика карточек недоступна для этих матчей")

    def get_shots(self, team: str, count: int = 10) -> Stat[ShotsStat]:
        if team == "Mock Home FC":
            return Stat.ok(ShotsStat(avg_total=13.7, avg_on_target=6.0, matches_counted=3))
        return Stat.missing("Детальная статистика ударов недоступна для этих матчей")

    def get_standings(self, league: str) -> Stat[List[StandingRow]]:
        if league != "Mock League":
            return Stat.missing(f"Мок-провайдер не содержит таблицу для лиги «{league}»")
        return Stat.ok([
            StandingRow(team="Mock Home FC", rank=3, points=55, played=28),
            StandingRow(team="Mock Away FC", rank=9, points=40, played=28),
        ])

    def get_lineups(self, home_team: str, away_team: str) -> Stat[List[TeamLineup]]:
        # Sample-only: lineups are typically unpublished until close to
        # kickoff, so the mock demonstrates the "not yet available" case.
        return Stat.missing("Составы ещё не опубликованы (демонстрация состояния «недоступно»)")

    def get_injuries(self, team: str) -> Stat[List[InjuryEntry]]:
        unknown = self._check_known(team)
        if unknown:
            return unknown
        if team == "Mock Home FC":
            return Stat.ok([InjuryEntry(player="J. Striker (mock)", reason="Hamstring")])
        return Stat.ok([])  # legitimate "no injuries reported", not missing
