"""
API-Football provider -- real implementation (v3.football.api-sports.io).

Activated for the "AI predictions" live feature. Every method still
follows the contract in football/interface.py: real data becomes
Stat.ok(...), anything that could not be retrieved (missing team, no
matches, HTTP error, exhausted request budget, ...) becomes
Stat.missing(reason) -- never a guessed/fabricated value.

Credit protection: this provider enforces its own request budget
(`max_requests_per_run`, default 200) *and* watches the
`x-ratelimit-requests-remaining` response header API-Football sends back.
Once either limit is hit, every subsequent call in this run short-circuits
to Stat.missing(...) without touching the network, so one expensive
prediction run can never silently exhaust the whole daily quota.

To bound the number of network calls per match, per-fixture statistics
(corners/fouls/cards/shots) are only fetched for the most recent
`stats_lookback` fixtures (default 3) of each team, not the full
`get_last_matches` window -- a smaller sample naturally lowers
sample_reliability in the selection engine rather than costing 10 API
calls per match.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

import requests

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

BASE_URL = "https://v3.football.api-sports.io"
FINISHED_STATUSES = {"FT", "AET", "PEN"}

_QUOTA_EXHAUSTED_REASON = "Достигнут лимит запросов к API-Football для этого запуска"


def _season_for(now: Optional[datetime.datetime] = None) -> int:
    """European-style season year heuristic: seasons that span two calendar
    years (Aug-May) are referenced by their starting year. From July
    onward we are already inside the *new* season."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


class ApiFootballProvider(FootballStatisticsProvider):
    name = "api_football"

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        max_requests_per_run: int = 200,
        stats_lookback: int = 3,
        season: Optional[int] = None,
        now: Optional[datetime.datetime] = None,
        session: Optional[Any] = None,
    ) -> None:
        self.api_key = api_key
        self.max_requests_per_run = max_requests_per_run
        self.stats_lookback = stats_lookback
        self.season = season if season is not None else _season_for(now)
        self._session = session or requests
        self.requests_made = 0
        self.remaining_quota: Optional[int] = None
        self._quota_exhausted = False

        # Per-run caches -- never persisted across process restarts; each
        # cache entry only ever holds real retrieved data or is simply
        # absent (a miss triggers a fresh call, never a fabricated value).
        self._team_id_cache: Dict[str, Optional[int]] = {}
        self._league_id_cache: Dict[str, Optional[int]] = {}
        self._last_matches_cache: Dict[tuple, Stat] = {}
        self._fixture_stats_cache: Dict[int, Optional[Dict[str, Any]]] = {}
        self._standings_cache: Dict[str, Stat] = {}
        self._injuries_cache: Dict[str, Stat] = {}

    # -- low-level HTTP -----------------------------------------------------

    def _budget_available(self) -> bool:
        if self._quota_exhausted:
            return False
        if self.requests_made >= self.max_requests_per_run:
            self._quota_exhausted = True
            return False
        if self.remaining_quota is not None and self.remaining_quota <= 0:
            self._quota_exhausted = True
            return False
        return True

    def _get(self, path: str, params: Dict[str, Any]) -> "tuple[Optional[List[Dict[str, Any]]], Optional[str]]":
        """Returns (response_list_or_None, error_reason_or_None)."""
        if not self.api_key:
            return None, "Не задан FOOTBALL_API_KEY"
        if not self._budget_available():
            return None, _QUOTA_EXHAUSTED_REASON

        self.requests_made += 1
        try:
            resp = self._session.get(
                f"{BASE_URL}{path}",
                params=params,
                headers={"x-apisports-key": self.api_key},
                timeout=20,
            )
        except requests.RequestException as exc:
            return None, f"Сетевая ошибка при обращении к API-Football: {exc}"

        remaining_header = resp.headers.get("x-ratelimit-requests-remaining")
        if remaining_header is not None:
            try:
                self.remaining_quota = int(remaining_header)
            except ValueError:
                pass

        if resp.status_code != 200:
            return None, f"API-Football вернул HTTP {resp.status_code}"

        try:
            payload = resp.json()
        except ValueError:
            return None, "API-Football вернул некорректный JSON"

        errors = payload.get("errors")
        if errors:
            return None, f"API-Football сообщил об ошибке: {errors}"

        return payload.get("response", []), None

    # -- team/league resolution ----------------------------------------------

    def search_teams(self, query: str) -> Stat[List[Dict[str, Any]]]:
        """Real, raw `/teams?search=` candidates (team id, name, country)
        for `query` -- unlike `_resolve_team_id` (which silently commits to
        the first hit), this returns every candidate the API found so a
        caller can score/confirm the real match itself (see
        ai_predictions/football_matching.py). Never guesses: an empty
        response is Stat.missing, not an invented team."""
        response, error = self._get("/teams", {"search": query})
        if error:
            return Stat.missing(error)
        if not response:
            return Stat.missing(f"Команда «{query}» не найдена в API-Football")
        candidates = [
            {
                "id": entry.get("team", {}).get("id"),
                "name": entry.get("team", {}).get("name", ""),
                "country": entry.get("team", {}).get("country"),
            }
            for entry in response
        ]
        return Stat.ok(candidates)

    def _resolve_team_id(self, team: str) -> "tuple[Optional[int], Optional[str]]":
        # Only a confirmed empty search result (a real "not found" answer
        # from the API) is cached permanently for this run. Transient
        # failures (rate limiting, network errors, HTTP 5xx, exhausted
        # budget) are deliberately NOT cached here -- caching them would
        # turn a temporary problem into a permanent false "team not found"
        # for the rest of the run, silently starving every market for that
        # team of real data.
        key = team.strip().lower()
        if key in self._team_id_cache:
            cached = self._team_id_cache[key]
            if cached is None:
                return None, f"Команда «{team}» не найдена в API-Football"
            return cached, None
        response, error = self._get("/teams", {"search": team})
        if error:
            return None, error
        if not response:
            self._team_id_cache[key] = None
            return None, f"Команда «{team}» не найдена в API-Football"
        team_id = response[0].get("team", {}).get("id")
        self._team_id_cache[key] = team_id
        return team_id, None

    def _resolve_league_id(self, league: str) -> "tuple[Optional[int], Optional[str]]":
        # See _resolve_team_id: only a confirmed empty result is cached as
        # permanently unresolved; transient errors are never cached.
        key = league.strip().lower()
        if key in self._league_id_cache:
            cached = self._league_id_cache[key]
            if cached is None:
                return None, f"Лига «{league}» не найдена в API-Football"
            return cached, None
        response, error = self._get("/leagues", {"search": league})
        if error:
            return None, error
        if not response:
            self._league_id_cache[key] = None
            return None, f"Лига «{league}» не найдена в API-Football"
        league_id = response[0].get("league", {}).get("id")
        self._league_id_cache[key] = league_id
        return league_id, None

    # -- fixture fetch/parse --------------------------------------------------

    def _fetch_fixtures(self, team_id: int, count: int, mode: str) -> "tuple[Optional[List[Dict[str, Any]]], Optional[str]]":
        params: Dict[str, Any] = {"team": team_id}
        if mode == "last":
            params["last"] = count
        else:
            params["next"] = count
        return self._get("/fixtures", params)

    @staticmethod
    def _to_match_summary(fixture: Dict[str, Any]) -> MatchSummary:
        f = fixture.get("fixture", {})
        league = fixture.get("league", {})
        teams = fixture.get("teams", {})
        goals = fixture.get("goals", {})
        score = fixture.get("score", {})
        ht = score.get("halftime", {}) or {}
        return MatchSummary(
            date=f.get("date", ""),
            home_team=teams.get("home", {}).get("name", ""),
            away_team=teams.get("away", {}).get("name", ""),
            home_goals=goals.get("home"),
            away_goals=goals.get("away"),
            ht_home_goals=ht.get("home"),
            ht_away_goals=ht.get("away"),
            competition=league.get("name"),
            venue=(f.get("venue") or {}).get("name"),
            status=(f.get("status") or {}).get("short"),
        )

    def _cached_finished_fixtures(self, team: str, count: int) -> "tuple[Optional[List[Dict[str, Any]]], Optional[str]]":
        cache_key = (team.strip().lower(), count)
        cached = self._last_matches_cache.get(cache_key)
        if cached is not None:
            if not cached.available:
                return None, cached.reason
            return cached.value, None

        team_id, error = self._resolve_team_id(team)
        if error:
            # Transient (rate limit / network / budget) errors are not
            # cached -- a retry within this same run may still succeed.
            return None, error

        # Ask API-Football for a few extra to allow for in-progress/postponed
        # entries that "last" sometimes still includes, then filter locally.
        response, error = self._fetch_fixtures(team_id, count + 5, "last")
        if error:
            return None, error

        finished = [fx for fx in response if (fx.get("fixture", {}).get("status", {}).get("short")) in FINISHED_STATUSES]
        finished = finished[:count]
        if not finished:
            reason = f"Нет завершённых матчей для «{team}» за запрошенный период"
            self._last_matches_cache[cache_key] = Stat.missing(reason)
            return None, reason

        self._last_matches_cache[cache_key] = Stat.ok(finished)
        return finished, None

    # -- interface methods ----------------------------------------------------

    def get_upcoming_matches(self, team: str, limit: int = 5) -> Stat[List[MatchSummary]]:
        team_id, error = self._resolve_team_id(team)
        if error:
            return Stat.missing(error)
        response, error = self._fetch_fixtures(team_id, limit, "next")
        if error:
            return Stat.missing(error)
        if not response:
            return Stat.missing(f"Нет предстоящих матчей для «{team}»")
        return Stat.ok([self._to_match_summary(fx) for fx in response])

    def get_last_matches(self, team: str, count: int = 10) -> Stat[List[MatchSummary]]:
        fixtures, error = self._cached_finished_fixtures(team, count)
        if error:
            return Stat.missing(error)
        return Stat.ok([self._to_match_summary(fx) for fx in fixtures])

    def get_home_away_form(self, team: str, count: int = 10) -> Stat[FormSplit]:
        fixtures, error = self._cached_finished_fixtures(team, count)
        if error:
            return Stat.missing(error)

        def result_letter(fixture: Dict[str, Any]) -> Optional[str]:
            teams = fixture.get("teams", {})
            is_home = teams.get("home", {}).get("name") == team
            home_win = teams.get("home", {}).get("winner")
            away_win = teams.get("away", {}).get("winner")
            if home_win is None and away_win is None:
                return "D"
            if is_home:
                return "W" if home_win else ("D" if home_win is None else "L")
            return "W" if away_win else ("D" if away_win is None else "L")

        # Chronological order oldest->newest, most recent last (per interface docstring).
        ordered = list(reversed(fixtures))
        overall = "".join(letter for fx in ordered if (letter := result_letter(fx)))
        home_only = "".join(
            letter for fx in ordered
            if fx.get("teams", {}).get("home", {}).get("name") == team and (letter := result_letter(fx))
        )
        away_only = "".join(
            letter for fx in ordered
            if fx.get("teams", {}).get("away", {}).get("name") == team and (letter := result_letter(fx))
        )
        return Stat.ok(FormSplit(overall=overall, home=home_only, away=away_only, matches_counted=len(fixtures)))

    def get_head_to_head(self, team_a: str, team_b: str, count: int = 10) -> Stat[List[MatchSummary]]:
        id_a, error_a = self._resolve_team_id(team_a)
        if error_a:
            return Stat.missing(error_a)
        id_b, error_b = self._resolve_team_id(team_b)
        if error_b:
            return Stat.missing(error_b)
        response, error = self._get("/fixtures/headtohead", {"h2h": f"{id_a}-{id_b}", "last": count})
        if error:
            return Stat.missing(error)
        finished = [fx for fx in response if fx.get("fixture", {}).get("status", {}).get("short") in FINISHED_STATUSES]
        if not finished:
            return Stat.missing(f"Нет личных встреч между «{team_a}» и «{team_b}»")
        return Stat.ok([self._to_match_summary(fx) for fx in finished[:count]])

    def get_goals_by_half(self, team: str, count: int = 10) -> Stat[GoalsByHalf]:
        fixtures, error = self._cached_finished_fixtures(team, count)
        if error:
            return Stat.missing(error)

        first_scored, first_conceded, second_scored, second_conceded = [], [], [], []
        for fx in fixtures:
            teams = fx.get("teams", {})
            goals = fx.get("goals", {})
            score = fx.get("score", {})
            ht = score.get("halftime") or {}
            is_home = teams.get("home", {}).get("name") == team
            ht_for = ht.get("home") if is_home else ht.get("away")
            ht_against = ht.get("away") if is_home else ht.get("home")
            ft_for = goals.get("home") if is_home else goals.get("away")
            ft_against = goals.get("away") if is_home else goals.get("home")
            if ht_for is None or ht_against is None or ft_for is None or ft_against is None:
                continue
            first_scored.append(ht_for)
            first_conceded.append(ht_against)
            second_scored.append(ft_for - ht_for)
            second_conceded.append(ft_against - ht_against)

        if not first_scored:
            return Stat.missing(f"Нет данных по таймам для «{team}»")

        def avg(values: List[int]) -> float:
            return sum(values) / len(values)

        return Stat.ok(GoalsByHalf(
            first_half_scored_avg=avg(first_scored),
            first_half_conceded_avg=avg(first_conceded),
            second_half_scored_avg=avg(second_scored),
            second_half_conceded_avg=avg(second_conceded),
            matches_counted=len(first_scored),
            intervals=None,
            intervals_reason="API-Football: детализация по интервалам не запрашивалась в этой реализации",
        ))

    def get_btts_frequency(self, team: str, count: int = 10) -> Stat[str]:
        fixtures, error = self._cached_finished_fixtures(team, count)
        if error:
            return Stat.missing(error)
        both_scored = 0
        counted = 0
        for fx in fixtures:
            goals = fx.get("goals", {})
            home_goals, away_goals = goals.get("home"), goals.get("away")
            if home_goals is None or away_goals is None:
                continue
            counted += 1
            if home_goals > 0 and away_goals > 0:
                both_scored += 1
        if counted == 0:
            return Stat.missing(f"Нет данных для BTTS у «{team}»")
        return Stat.ok(f"{both_scored}/{counted}")

    def get_clean_sheets(self, team: str, count: int = 10) -> Stat[CleanSheetStat]:
        fixtures, error = self._cached_finished_fixtures(team, count)
        if error:
            return Stat.missing(error)
        clean_sheets, failed_to_score, counted = 0, 0, 0
        for fx in fixtures:
            teams = fx.get("teams", {})
            goals = fx.get("goals", {})
            is_home = teams.get("home", {}).get("name") == team
            scored = goals.get("home") if is_home else goals.get("away")
            conceded = goals.get("away") if is_home else goals.get("home")
            if scored is None or conceded is None:
                continue
            counted += 1
            if conceded == 0:
                clean_sheets += 1
            if scored == 0:
                failed_to_score += 1
        if counted == 0:
            return Stat.missing(f"Нет данных о сухих матчах для «{team}»")
        return Stat.ok(CleanSheetStat(clean_sheets=clean_sheets, failed_to_score=failed_to_score, matches_counted=counted))

    # -- per-fixture statistics (corners/fouls/cards/shots) --------------------

    def _fixture_statistics(self, fixture_id: int, team_name: str) -> Optional[Dict[str, Any]]:
        """Returns the raw `statistics` list entry (dict of type->value) for
        `team_name` within `fixture_id`, or None if unavailable. Cached per
        fixture (a single call returns both teams' stats)."""
        if fixture_id not in self._fixture_stats_cache:
            response, error = self._get("/fixtures/statistics", {"fixture": fixture_id})
            if error:
                # Transient failure -- do not cache, allow a later retry.
                return None
            if not response:
                self._fixture_stats_cache[fixture_id] = None
            else:
                by_team = {}
                for entry in response:
                    name = entry.get("team", {}).get("name")
                    stats = {s.get("type"): s.get("value") for s in entry.get("statistics", [])}
                    by_team[name] = stats
                self._fixture_stats_cache[fixture_id] = by_team
        cached = self._fixture_stats_cache[fixture_id]
        if not cached:
            return None
        return cached.get(team_name)

    def _recent_fixture_ids(self, team: str) -> "tuple[List[int], Optional[str]]":
        fixtures, error = self._cached_finished_fixtures(team, self.stats_lookback)
        if error:
            return [], error
        ids = [fx.get("fixture", {}).get("id") for fx in fixtures if fx.get("fixture", {}).get("id") is not None]
        if not ids:
            return [], f"Нет ID матчей для детальной статистики «{team}»"
        return ids, None

    @staticmethod
    def _numeric(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, str):
            cleaned = value.replace("%", "").strip()
            if not cleaned or not cleaned.replace(".", "", 1).lstrip("-").isdigit():
                return None
            return float(cleaned)
        if isinstance(value, (int, float)):
            return float(value)
        return None

    def _average_stat_type(self, team: str, stat_type: str, label: str) -> Stat[AverageStat]:
        fixture_ids, error = self._recent_fixture_ids(team)
        if error:
            return Stat.missing(error)
        values = []
        for fid in fixture_ids:
            stats = self._fixture_statistics(fid, team)
            if not stats:
                continue
            v = self._numeric(stats.get(stat_type))
            if v is not None:
                values.append(v)
        if not values:
            return Stat.missing(f"Нет данных «{label}» для «{team}» по последним матчам")
        return Stat.ok(AverageStat(average=sum(values) / len(values), matches_counted=len(values)))

    def get_corners(self, team: str, count: int = 10) -> Stat[AverageStat]:
        return self._average_stat_type(team, "Corner Kicks", "угловые")

    def get_fouls(self, team: str, count: int = 10) -> Stat[AverageStat]:
        return self._average_stat_type(team, "Fouls", "фолы")

    def get_cards(self, team: str, count: int = 10) -> Stat[CardsStat]:
        fixture_ids, error = self._recent_fixture_ids(team)
        if error:
            return Stat.missing(error)
        yellows, reds, counted = [], [], 0
        for fid in fixture_ids:
            stats = self._fixture_statistics(fid, team)
            if not stats:
                continue
            y = self._numeric(stats.get("Yellow Cards"))
            r = self._numeric(stats.get("Red Cards"))
            if y is None and r is None:
                continue
            yellows.append(y or 0.0)
            reds.append(r or 0.0)
            counted += 1
        if counted == 0:
            return Stat.missing(f"Нет данных о карточках для «{team}»")
        return Stat.ok(CardsStat(
            avg_yellow=sum(yellows) / counted, avg_red=sum(reds) / counted, matches_counted=counted,
        ))

    def get_shots(self, team: str, count: int = 10) -> Stat[ShotsStat]:
        fixture_ids, error = self._recent_fixture_ids(team)
        if error:
            return Stat.missing(error)
        totals, on_target, counted = [], [], 0
        for fid in fixture_ids:
            stats = self._fixture_statistics(fid, team)
            if not stats:
                continue
            total = self._numeric(stats.get("Total Shots"))
            on = self._numeric(stats.get("Shots on Goal"))
            if total is None and on is None:
                continue
            totals.append(total or 0.0)
            on_target.append(on or 0.0)
            counted += 1
        if counted == 0:
            return Stat.missing(f"Нет данных об ударах для «{team}»")
        return Stat.ok(ShotsStat(
            avg_total=sum(totals) / counted, avg_on_target=sum(on_target) / counted, matches_counted=counted,
        ))

    # -- standings / lineups / injuries ---------------------------------------

    def get_standings(self, league: str) -> Stat[List[StandingRow]]:
        key = league.strip().lower()
        if key in self._standings_cache:
            return self._standings_cache[key]
        league_id, error = self._resolve_league_id(league)
        if error:
            # Transient errors are not cached -- a retry within this run
            # may still succeed once rate limiting clears.
            return Stat.missing(error)
        response, error = self._get("/standings", {"league": league_id, "season": self.season})
        if error:
            return Stat.missing(error)
        try:
            table = response[0]["league"]["standings"][0]
        except (IndexError, KeyError, TypeError):
            result = Stat.missing(f"Таблица лиги «{league}» недоступна")
            self._standings_cache[key] = result
            return result
        rows = [
            StandingRow(
                team=row.get("team", {}).get("name", ""),
                rank=row.get("rank", 0),
                points=row.get("points", 0),
                played=row.get("all", {}).get("played", 0),
            )
            for row in table
        ]
        result = Stat.ok(rows)
        self._standings_cache[key] = result
        return result

    def get_lineups(self, home_team: str, away_team: str) -> Stat[List[TeamLineup]]:
        home_id, error = self._resolve_team_id(home_team)
        if error:
            return Stat.missing(error)
        response, error = self._fetch_fixtures(home_id, 1, "next")
        if error:
            return Stat.missing(error)
        matching_fixture = None
        for fx in response or []:
            teams = fx.get("teams", {})
            names = {teams.get("home", {}).get("name"), teams.get("away", {}).get("name")}
            if away_team in names:
                matching_fixture = fx
                break
        if matching_fixture is None:
            return Stat.missing(f"Не найден предстоящий матч {home_team} — {away_team}")
        fixture_id = matching_fixture.get("fixture", {}).get("id")
        response, error = self._get("/fixtures/lineups", {"fixture": fixture_id})
        if error:
            return Stat.missing(error)
        if not response:
            return Stat.missing("Составы пока не опубликованы")
        lineups = [
            TeamLineup(
                team=entry.get("team", {}).get("name", ""),
                formation=entry.get("formation"),
                starters=[
                    LineupPlayer(name=p.get("player", {}).get("name", ""), position=p.get("player", {}).get("pos"))
                    for p in entry.get("startXI", [])
                ],
            )
            for entry in response
        ]
        return Stat.ok(lineups)

    def get_injuries(self, team: str) -> Stat[List[InjuryEntry]]:
        key = team.strip().lower()
        if key in self._injuries_cache:
            return self._injuries_cache[key]
        team_id, error = self._resolve_team_id(team)
        if error:
            # Transient errors are not cached -- a retry within this run
            # may still succeed once rate limiting clears.
            return Stat.missing(error)
        response, error = self._get("/injuries", {"team": team_id, "season": self.season})
        if error:
            return Stat.missing(error)
        if not response:
            result = Stat.ok([])  # explicitly known: no reported injuries
            self._injuries_cache[key] = result
            return result
        entries = [
            InjuryEntry(player=e.get("player", {}).get("name", ""), reason=e.get("player", {}).get("reason"))
            for e in response
        ]
        result = Stat.ok(entries)
        self._injuries_cache[key] = result
        return result
