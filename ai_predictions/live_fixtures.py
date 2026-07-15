"""
Live in-play fixture discovery (Live mode, 2026-07-15) -- real matches
already in progress right now, sourced from API-Football's
`/fixtures?live=all` endpoint. Completely independent of
ai_predictions/fixtures.py's pre-match 36h window discovery: never shares
its cache keys, never touches the shared daily archive/pool.

Never fabricates a fixture: a fixture is only included if it carries one
of value_config.FIXTURE_LIVE_STATUSES and has two real team names and a
real kickoff timestamp. If the network/quota call itself fails, that is
recorded honestly in `errors` -- a failed call is never treated as "zero
matches live right now".
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ai_predictions.value_config import FIXTURE_LIVE_STATUSES
from ai_predictions.window import parse_commence_time


@dataclass
class LiveFixture:
    fixture_id: int
    kickoff_utc: datetime.datetime  # real scheduled kickoff (used only for display/matching)
    home_team: str
    away_team: str
    league_name: Optional[str]
    league_country: Optional[str]
    status_short: str
    elapsed_minutes: Optional[int]
    home_score: Optional[int]
    away_score: Optional[int]


@dataclass
class LiveFixtureDiscoveryResult:
    fixtures: List[LiveFixture] = field(default_factory=list)
    total_raw_fixtures: int = 0
    excluded_missing_fields: int = 0
    requests_used: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True if the real call itself succeeded (even with zero live
        matches right now) -- distinguishes "nothing live at the moment"
        from "could not even ask API-Football"."""
        return not self.errors


def _parse_live_fixture(raw: Dict[str, Any]) -> Optional[LiveFixture]:
    f = raw.get("fixture", {}) or {}
    league = raw.get("league", {}) or {}
    teams = raw.get("teams", {}) or {}
    goals = raw.get("goals", {}) or {}
    home = teams.get("home", {}) or {}
    away = teams.get("away", {}) or {}
    home_name = home.get("name")
    away_name = away.get("name")
    fixture_id = f.get("id")
    kickoff = parse_commence_time(f.get("date"))
    status = f.get("status", {}) or {}
    status_short = status.get("short") or ""
    if fixture_id is None or not home_name or not away_name or kickoff is None:
        return None
    return LiveFixture(
        fixture_id=fixture_id,
        kickoff_utc=kickoff,
        home_team=home_name,
        away_team=away_name,
        league_name=league.get("name"),
        league_country=league.get("country"),
        status_short=status_short,
        elapsed_minutes=status.get("elapsed"),
        home_score=goals.get("home"),
        away_score=goals.get("away"),
    )


def discover_live_fixtures(
    api_key: Optional[str],
    cache,
    *,
    provider_factory=None,
) -> LiveFixtureDiscoveryResult:
    """Fetches every real, currently in-progress fixture from
    API-Football. Guarded by the same shared daily quota reserve as the
    pre-match pipeline (`cache.can_spend`/`cache.record_requests`) -- Live
    mode competes for the same budget, it does not get its own separate
    allowance."""
    result = LiveFixtureDiscoveryResult()

    if not api_key:
        result.errors.append("Не задан FOOTBALL_API_KEY")
        return result

    if not cache.can_spend(1):
        result.errors.append("Резерв запросов к API-Football на сегодня исчерпан")
        return result

    if provider_factory is None:
        from football.providers.api_football import ApiFootballProvider
        provider_factory = lambda: ApiFootballProvider(api_key=api_key)

    provider = provider_factory()
    stat = provider.get_live_fixtures()
    cache.record_requests(1)
    result.requests_used += 1

    if not stat.available:
        result.errors.append(stat.reason or "API-Football не вернул данные о текущих матчах")
        return result

    raw_fixtures = stat.value or []
    result.total_raw_fixtures = len(raw_fixtures)
    for raw in raw_fixtures:
        status_short = ((raw.get("fixture", {}) or {}).get("status", {}) or {}).get("short") or ""
        if status_short not in FIXTURE_LIVE_STATUSES:
            # /fixtures?live=all should only ever return live statuses, but
            # never trust that blindly -- anything else is excluded, not
            # guessed to be live.
            result.excluded_missing_fields += 1
            continue
        fixture = _parse_live_fixture(raw)
        if fixture is None:
            result.excluded_missing_fields += 1
            continue
        result.fixtures.append(fixture)

    return result
