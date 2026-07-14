"""
Unit tests for ai_predictions/league_relevance.py: scoping The Odds API
sport-key queries to only what discovered real fixtures plausibly need.
"""

import datetime
import sys

sys.path.insert(0, ".")

from ai_predictions.fixtures import Fixture
from ai_predictions.league_relevance import select_relevant_sport_keys

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


NOW = datetime.datetime(2026, 7, 14, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _fixture(league_name, league_country):
    return Fixture(
        fixture_id=1, kickoff_utc=NOW, home_team="A", away_team="B",
        home_team_id=1, away_team_id=2, league_name=league_name, league_country=league_country,
        status_short="NS",
    )


CATALOG = [
    {"key": "soccer_epl", "title": "EPL", "description": "England - Premier League"},
    {"key": "soccer_norway_eliteserien", "title": "Eliteserien", "description": "Norway - Eliteserien"},
    {"key": "soccer_brazil_campeonato", "title": "Brasileirao", "description": "Brazil - Serie A"},
    {"key": "basketball_nba", "title": "NBA", "description": "USA - NBA"},
]


def test_no_fixtures_yields_no_relevant_keys():
    result = select_relevant_sport_keys([], CATALOG)
    check("empty fixture list never scopes to anything (nothing to look for)", result == [], result)


def test_always_relevant_top_league_included_even_without_direct_match():
    fixtures = [_fixture("Norway Eliteserien", "Norway")]
    result = select_relevant_sport_keys(fixtures, CATALOG)
    check("EPL always included as a top league", "soccer_epl" in result, result)


def test_country_match_includes_the_right_league():
    fixtures = [_fixture("Eliteserien", "Norway")]
    result = select_relevant_sport_keys(fixtures, CATALOG)
    check("Norwegian fixture pulls in the Norwegian sport key", "soccer_norway_eliteserien" in result, result)


def test_unrelated_sport_never_included():
    fixtures = [_fixture("Eliteserien", "Norway")]
    result = select_relevant_sport_keys(fixtures, CATALOG)
    check("basketball never scoped in from a football fixture", "basketball_nba" not in result, result)


def test_country_with_no_catalog_entry_is_simply_not_scoped():
    fixtures = [_fixture("Some Obscure League", "Nowhereland")]
    result = select_relevant_sport_keys(fixtures, CATALOG)
    # Top leagues are always included regardless; nothing invented for
    # "Nowhereland" itself.
    check("no fabricated key for an unmatched country", "soccer_brazil_campeonato" not in result, result)


def run():
    test_no_fixtures_yields_no_relevant_keys()
    test_always_relevant_top_league_included_even_without_direct_match()
    test_country_match_includes_the_right_league()
    test_unrelated_sport_never_included()
    test_country_with_no_catalog_entry_is_simply_not_scoped()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
