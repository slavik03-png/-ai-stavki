"""
Phase 1 -- real fixture discovery for the strict 36h analysis window,
sourced from API-Football (not a hardcoded league list, not "today/
tomorrow" shortcuts). This is the primary source of "what real matches
exist right now" for the production value pipeline; The Odds API is only
ever queried afterwards, scoped to what was discovered here
(ai_predictions/league_relevance.py).

Design notes:
- The window is [now, now + WINDOW_HOURS) in UTC. To cover it we fetch
  every *Yekaterinburg calendar date* the window touches (API-Football's
  `date` param is evaluated against fixture kickoff, and a UTC day can
  span two Yekaterinburg dates and vice versa -- fetching by UTC date
  would risk silently missing fixtures near midnight, so we fetch the
  union of both UTC and Yekaterinburg calendar dates the window touches
  to be safe. Duplicate fixture IDs across dates are deduped.
- Every real network call is 24h-cached (ai_predictions/football_cache.py)
  keyed by date, and gated by the shared daily quota reserve -- fixture
  discovery competes for the same budget as enrichment.
- A fixture is only "discovered" if it (a) carries one of the
  not-started statuses, (b) has two real team names, and (c) its own
  reported kickoff timestamp falls inside the window. Live/finished/
  cancelled fixtures, and fixtures whose real kickoff is outside the
  window even if returned for a queried date, are excluded and counted,
  never silently dropped.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from ai_predictions.football_cache import FootballCache
from ai_predictions.value_config import (
    FIXTURE_CANCELLED_STATUSES,
    FIXTURE_FINISHED_STATUSES,
    FIXTURE_LIST_CACHE_TTL_HOURS,
    FIXTURE_LIVE_STATUSES,
    FIXTURE_NOT_STARTED_STATUSES,
    FIXTURE_POSTPONED_STATUSES,
)
from ai_predictions.window import DISPLAY_TZ, WINDOW_HOURS, parse_commence_time


@dataclass
class Fixture:
    fixture_id: int
    kickoff_utc: datetime.datetime
    home_team: str
    away_team: str
    home_team_id: Optional[int]
    away_team_id: Optional[int]
    league_name: Optional[str]
    league_country: Optional[str]
    status_short: str


@dataclass
class FixtureDiscoveryResult:
    fixtures: List[Fixture] = field(default_factory=list)
    dates_queried: List[str] = field(default_factory=list)
    dates_from_cache: int = 0
    dates_from_network: int = 0
    requests_used: int = 0
    total_raw_fixtures: int = 0
    excluded_by_status: int = 0
    excluded_by_window: int = 0
    excluded_missing_teams: int = 0
    quota_blocked_dates: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True if at least one date query actually succeeded (cache or
        network) -- distinguishes "zero real fixtures found" from
        "discovery itself failed" for honest reporting."""
        return len(self.errors) < len(self.dates_queried) or bool(self.fixtures)


def _window_dates(now: datetime.datetime, window_hours: float = WINDOW_HOURS) -> List[str]:
    """Every distinct UTC and Yekaterinburg calendar date the
    [now, now+window_hours) window touches, as 'YYYY-MM-DD' strings."""
    end = now + datetime.timedelta(hours=window_hours)
    dates: Set[str] = set()
    cursor = now
    while True:
        dates.add(cursor.astimezone(datetime.timezone.utc).date().isoformat())
        dates.add(cursor.astimezone(DISPLAY_TZ).date().isoformat())
        if cursor >= end:
            break
        cursor = min(cursor + datetime.timedelta(hours=12), end)
    dates.add(end.astimezone(datetime.timezone.utc).date().isoformat())
    dates.add(end.astimezone(DISPLAY_TZ).date().isoformat())
    return sorted(dates)


def _parse_fixture(raw: Dict[str, Any]) -> Optional[Fixture]:
    f = raw.get("fixture", {}) or {}
    league = raw.get("league", {}) or {}
    teams = raw.get("teams", {}) or {}
    home = teams.get("home", {}) or {}
    away = teams.get("away", {}) or {}
    home_name = home.get("name")
    away_name = away.get("name")
    fixture_id = f.get("id")
    kickoff = parse_commence_time(f.get("date"))
    status_short = (f.get("status") or {}).get("short") or ""
    if fixture_id is None or not home_name or not away_name or kickoff is None:
        return None
    return Fixture(
        fixture_id=fixture_id,
        kickoff_utc=kickoff,
        home_team=home_name,
        away_team=away_name,
        home_team_id=home.get("id"),
        away_team_id=away.get("id"),
        league_name=league.get("name"),
        league_country=league.get("country"),
        status_short=status_short,
    )


def discover_fixtures_in_window(
    api_key: Optional[str],
    cache: FootballCache,
    now: datetime.datetime,
    window_hours: float = WINDOW_HOURS,
    *,
    provider_factory=None,
) -> FixtureDiscoveryResult:
    """Returns every real, not-yet-started fixture whose kickoff falls in
    [now, now+window_hours). Never fabricates a fixture; a date that could
    not be fetched (quota, network, provider error) is recorded in
    `errors`/`quota_blocked_dates`, not silently treated as "no matches"."""
    result = FixtureDiscoveryResult()

    if not api_key:
        result.errors.append("Не задан FOOTBALL_API_KEY")
        return result

    if provider_factory is None:
        from football.providers.api_football import ApiFootballProvider
        provider_factory = lambda: ApiFootballProvider(api_key=api_key, now=now)

    provider = provider_factory()
    dates = _window_dates(now, window_hours)
    result.dates_queried = dates

    seen_ids: Set[int] = set()
    window_end = now + datetime.timedelta(hours=window_hours)

    for date_str in dates:
        cache_key = f"fixtures:date:{date_str}"
        cached = cache.get(cache_key, ttl_hours=FIXTURE_LIST_CACHE_TTL_HOURS)
        raw_fixtures: Optional[List[Dict[str, Any]]] = None
        if cached is not None:
            raw_fixtures = cached
            result.dates_from_cache += 1
        else:
            if not cache.can_spend(1):
                result.quota_blocked_dates.append(date_str)
                result.errors.append(
                    f"Резерв запросов к API-Football исчерпан, дата {date_str} не запрошена"
                )
                continue
            stat = provider.get_fixtures_by_date(date_str)
            cache.record_requests(1)
            result.requests_used += 1
            if not stat.available:
                result.errors.append(f"{date_str}: {stat.reason}")
                continue
            raw_fixtures = stat.value
            cache.set(cache_key, raw_fixtures)
            result.dates_from_network += 1

        result.total_raw_fixtures += len(raw_fixtures or [])
        for raw in raw_fixtures or []:
            status_short = ((raw.get("fixture", {}) or {}).get("status", {}) or {}).get("short") or ""
            if status_short in FIXTURE_LIVE_STATUSES or status_short in FIXTURE_FINISHED_STATUSES \
                    or status_short in FIXTURE_CANCELLED_STATUSES:
                result.excluded_by_status += 1
                continue
            if status_short not in FIXTURE_NOT_STARTED_STATUSES and status_short not in FIXTURE_POSTPONED_STATUSES:
                # Unknown/other status codes are excluded conservatively --
                # never guessed as "probably fine".
                result.excluded_by_status += 1
                continue
            fixture = _parse_fixture(raw)
            if fixture is None:
                result.excluded_missing_teams += 1
                continue
            if fixture.fixture_id in seen_ids:
                continue
            if not (now < fixture.kickoff_utc <= window_end):
                result.excluded_by_window += 1
                continue
            seen_ids.add(fixture.fixture_id)
            result.fixtures.append(fixture)

    result.fixtures.sort(key=lambda fx: fx.kickoff_utc)
    return result
