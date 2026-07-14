"""
Unit tests for the dynamic football-competition discovery added to
ai_predictions/odds_client.py: discover_football_sport_keys (filters The
Odds API's live /sports catalog to active, non-outrights Soccer entries),
and fetch_all_active_football_events (discovery + fetch merged into one
event pool). Also proves the existing strict 36h window filter is never
widened just because more sports are queried.

The real network layer (fetch_active_sports / _fetch_one_league) is
monkeypatched throughout -- no real HTTP calls happen in this file.
"""

import datetime
import sys

sys.path.insert(0, ".")

import ai_predictions.odds_client as odds_client_mod
from ai_predictions.odds_client import (
    SportsDiscovery,
    discover_football_sport_keys,
    fetch_all_active_football_events,
)
from ai_predictions.window import filter_events_in_window

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


FAKE_SPORTS_CATALOG = [
    {"key": "soccer_epl", "group": "Soccer", "active": True, "has_outrights": False},
    {"key": "soccer_norway_eliteserien", "group": "Soccer", "active": True, "has_outrights": False},
    {"key": "soccer_fifa_world_cup_winner", "group": "Soccer", "active": True, "has_outrights": True},
    {"key": "soccer_brazil_serie_b", "group": "Soccer", "active": False, "has_outrights": False},
    {"key": "basketball_nba", "group": "Basketball", "active": True, "has_outrights": False},
]


def _with_fake_sports_catalog(catalog, error=None):
    original = odds_client_mod.fetch_active_sports
    odds_client_mod.fetch_active_sports = (
        lambda api_key=None, persistent_cache=None: (None, error) if error else (catalog, None)
    )
    return original


def test_discovery_includes_only_active_non_outright_football():
    original = _with_fake_sports_catalog(FAKE_SPORTS_CATALOG)
    try:
        discovery = discover_football_sport_keys(api_key="fake-key")
    finally:
        odds_client_mod.fetch_active_sports = original

    check("discovers both active football leagues, not just majors",
          set(discovery.included) == {"soccer_epl", "soccer_norway_eliteserien"}, discovery.included)
    check("outrights-only competition is skipped with a real reason",
          "аутрайт" in discovery.skipped.get("soccer_fifa_world_cup_winner", "").lower())
    check("inactive competition is skipped with a real reason",
          "неактив" in discovery.skipped.get("soccer_brazil_serie_b", "").lower())
    check("non-football sport is never included",
          "basketball_nba" not in discovery.included and "basketball_nba" not in discovery.skipped)
    check("source is reported as the live API, not the fallback",
          discovery.source == "api")


def test_discovery_falls_back_to_hardcoded_list_on_api_failure():
    original = _with_fake_sports_catalog(None, error="Сетевая ошибка The Odds API (список видов спорта): timeout")
    try:
        discovery = discover_football_sport_keys(api_key="fake-key")
    finally:
        odds_client_mod.fetch_active_sports = original

    check("falls back to the hardcoded major-league list on discovery failure",
          discovery.included == odds_client_mod.FOOTBALL_SPORT_KEYS, discovery.included)
    check("fallback never silently hides the failure",
          discovery.discovery_error is not None and discovery.source == "fallback_hardcoded")


def _h2h_event(event_id, sport_key, hours_ahead):
    now = datetime.datetime(2026, 7, 13, 12, 0, 0, tzinfo=datetime.timezone.utc)
    return {
        "id": event_id,
        "sport_title": sport_key,
        "commence_time": (now + datetime.timedelta(hours=hours_ahead)).isoformat(),
        "home_team": "Home FC",
        "away_team": "Away FC",
        "bookmakers": [],
    }


def test_events_from_multiple_sports_merge_into_one_pool():
    discovery_catalog = [
        {"key": "soccer_epl", "group": "Soccer", "active": True, "has_outrights": False},
        {"key": "soccer_norway_eliteserien", "group": "Soccer", "active": True, "has_outrights": False},
    ]
    original_sports = _with_fake_sports_catalog(discovery_catalog)

    def fake_fetch_football_events(api_key=None, sport_keys=None, persistent_cache=None):
        events = []
        for key in sport_keys:
            e = _h2h_event(f"evt-{key}", key, hours_ahead=5)
            e["_sport_key"] = key
            events.append(e)
        return events, "400", []

    original_fetch = odds_client_mod.fetch_football_events
    odds_client_mod.fetch_football_events = fake_fetch_football_events
    try:
        result = fetch_all_active_football_events(api_key="fake-key")
    finally:
        odds_client_mod.fetch_active_sports = original_sports
        odds_client_mod.fetch_football_events = original_fetch

    found_sport_keys = {e["_sport_key"] for e in result.events}
    check("events from every discovered football sport are merged into one pool",
          found_sport_keys == {"soccer_epl", "soccer_norway_eliteserien"}, found_sport_keys)
    check("both sports are reported as successfully queried",
          set(result.sports_queried) == {"soccer_epl", "soccer_norway_eliteserien"}, result.sports_queried)
    check("no sport is reported as failed when both succeeded",
          result.sports_failed == {})


def test_one_failed_sport_does_not_lose_the_others():
    discovery_catalog = [
        {"key": "soccer_epl", "group": "Soccer", "active": True, "has_outrights": False},
        {"key": "soccer_norway_eliteserien", "group": "Soccer", "active": True, "has_outrights": False},
    ]
    original_sports = _with_fake_sports_catalog(discovery_catalog)

    def fake_fetch_football_events(api_key=None, sport_keys=None, persistent_cache=None):
        e = _h2h_event("evt-epl", "soccer_epl", hours_ahead=5)
        e["_sport_key"] = "soccer_epl"
        errors = ["The Odds API вернул HTTP 500 для soccer_norway_eliteserien"]
        return [e], "400", errors

    original_fetch = odds_client_mod.fetch_football_events
    odds_client_mod.fetch_football_events = fake_fetch_football_events
    try:
        result = fetch_all_active_football_events(api_key="fake-key")
    finally:
        odds_client_mod.fetch_active_sports = original_sports
        odds_client_mod.fetch_football_events = original_fetch

    check("the successful sport's events are still returned",
          any(e["_sport_key"] == "soccer_epl" for e in result.events))
    check("the failed sport is recorded with its real error, not silently dropped",
          "soccer_norway_eliteserien" in result.sports_failed, result.sports_failed)
    check("the failed sport is not counted among successfully queried sports",
          "soccer_norway_eliteserien" not in result.sports_queried, result.sports_queried)


def test_strict_36h_window_is_never_widened_by_more_sports():
    now = datetime.datetime(2026, 7, 13, 12, 0, 0, tzinfo=datetime.timezone.utc)
    events = [
        _h2h_event("evt-epl-in", "soccer_epl", hours_ahead=10),
        _h2h_event("evt-norway-in", "soccer_norway_eliteserien", hours_ahead=35),
        _h2h_event("evt-epl-out", "soccer_epl", hours_ahead=37),
        _h2h_event("evt-norway-out", "soccer_norway_eliteserien", hours_ahead=200),
        _h2h_event("evt-past", "soccer_epl", hours_ahead=-1),
    ]
    in_window, excluded_count = filter_events_in_window(events, now)
    in_window_ids = {e["id"] for e in in_window}
    check("only events strictly inside the 36h window survive regardless of how many sports were queried",
          in_window_ids == {"evt-epl-in", "evt-norway-in"}, in_window_ids)
    check("every out-of-window event across every sport is excluded, not just the majors",
          excluded_count == 3, excluded_count)


def run():
    test_discovery_includes_only_active_non_outright_football()
    test_discovery_falls_back_to_hardcoded_list_on_api_failure()
    test_events_from_multiple_sports_merge_into_one_pool()
    test_one_failed_sport_does_not_lose_the_others()
    test_strict_36h_window_is_never_widened_by_more_sports()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
