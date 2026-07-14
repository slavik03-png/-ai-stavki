"""
Unit tests for ai_predictions/enrichment.py's fixture-discovery-first
enrichment path (enrich_matched_candidates): the ApiFootballProvider is
monkeypatched -- no real HTTP calls happen in this file.
"""

import datetime
import os
import sys
import tempfile

sys.path.insert(0, ".")

import ai_predictions.enrichment as enrichment_mod
from ai_predictions.enrichment import enrich_matched_candidates
from ai_predictions.fixtures import Fixture
from ai_predictions.football_cache import FootballCache
from ai_predictions.matching import (
    ValidationStats,
    dedupe_bookmaker_rows,
    extract_rows,
    group_rows,
    raw_bookmaker_row_counts,
    validate_rows,
)
from ai_predictions.value_engine import build_value_candidates_from_groups
from football.interface import Stat

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


NOW = datetime.datetime(2026, 7, 14, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _event_with_bookmakers(prices, event_id="evt-1", home="Home FC", away="Away FC"):
    return {
        "id": event_id,
        "_sport_key": "soccer_epl",
        "home_team": home,
        "away_team": away,
        "commence_time": "2026-07-14T18:00:00Z",
        "bookmakers": [
            {
                "title": title,
                "last_update": "2026-07-14T10:00:00Z",
                "markets": [{
                    "key": "h2h",
                    "outcomes": [
                        {"name": home, "price": h},
                        {"name": "Draw", "price": d},
                        {"name": away, "price": a},
                    ],
                }],
            }
            for title, h, d, a in prices
        ],
    }


def _candidates_for(event):
    rows = extract_rows(event, event_id=event["id"], league="Test League")
    stats = ValidationStats()
    valid = validate_rows(rows, stats)
    raw_counts = raw_bookmaker_row_counts(valid)
    deduped = dedupe_bookmaker_rows(valid, stats)
    groups = group_rows(deduped)
    return build_value_candidates_from_groups(groups, raw_counts)


PRICES = [
    ("BookA", 2.00, 3.30, 4.00),
    ("BookB", 1.98, 3.35, 4.05),
    ("BookC", 2.02, 3.25, 3.95),
    ("BookD", 2.15, 3.15, 3.75),
]


class _FormStat:
    def __init__(self, overall, matches_counted):
        self.overall = overall
        self.matches_counted = matches_counted


class _FakeProvider:
    def __init__(self, form_by_team):
        self.form_by_team = form_by_team
        self.calls = []

    def get_home_away_form(self, team_name):
        self.calls.append(team_name)
        form = self.form_by_team.get(team_name)
        if form is None:
            return Stat.missing("no data")
        return Stat.ok(_FormStat(form, len(form)))


def _fixture(event, fixture_id=1001):
    return Fixture(
        fixture_id=fixture_id,
        kickoff_utc=datetime.datetime(2026, 7, 14, 18, 0, 0, tzinfo=datetime.timezone.utc),
        home_team=event["home_team"], away_team=event["away_team"],
        home_team_id=1, away_team_id=2, league_name="Premier League", league_country="England",
        status_short="NS",
    )


def test_missing_api_key_stays_market_only_and_reports_why():
    event = _event_with_bookmakers(PRICES)
    candidates = _candidates_for(event)
    home = next(c for c in candidates if c.selection == "Home FC")
    fixtures_by_event_id = {event["id"]: _fixture(event)}
    with tempfile.TemporaryDirectory() as d:
        cache = FootballCache(db_path=os.path.join(d, "c.db"), now=NOW)
        summary = enrich_matched_candidates(candidates, fixtures_by_event_id, api_key=None, cache=cache, now=NOW)
        check("no API key -> honest skipped_reason, not a silent success", summary.skipped_reason is not None, summary.skipped_reason)
        check("candidate stays market-only (no fabricated statistics)", home.statistics_probability is None)
        check("estimated_probability still populated from the market", home.estimated_probability is not None)


def test_real_form_data_blends_into_estimated_probability():
    event = _event_with_bookmakers(PRICES)
    candidates = _candidates_for(event)
    home = next(c for c in candidates if c.selection == "Home FC")
    fixtures_by_event_id = {event["id"]: _fixture(event)}
    provider = _FakeProvider({"Home FC": "WWWWWWWW", "Away FC": "LLLLLLLL"})

    original_provider_cls = enrichment_mod.ApiFootballProvider
    enrichment_mod.ApiFootballProvider = lambda api_key, now: provider
    try:
        with tempfile.TemporaryDirectory() as d:
            cache = FootballCache(db_path=os.path.join(d, "c.db"), now=NOW)
            summary = enrich_matched_candidates(candidates, fixtures_by_event_id, api_key="fake-key", cache=cache, now=NOW)
    finally:
        enrichment_mod.ApiFootballProvider = original_provider_cls

    check("strong home form retrieved -> event counted as blended", summary.blended_events == 1, summary.blended_events)
    check("statistics_probability populated for the home selection", home.statistics_probability is not None, home.statistics_probability)
    check("estimated_probability shifted above the pure market consensus for a dominant home team",
          home.estimated_probability > home.consensus_probability,
          (home.estimated_probability, home.consensus_probability))
    check("fixture_id attached to the enriched candidate", home.fixture_id == 1001, home.fixture_id)


def test_team_with_zero_real_matches_never_guessed_as_neutral():
    event = _event_with_bookmakers(PRICES)
    candidates = _candidates_for(event)
    home = next(c for c in candidates if c.selection == "Home FC")
    fixtures_by_event_id = {event["id"]: _fixture(event)}
    provider = _FakeProvider({})  # neither team has any real form data

    original_provider_cls = enrichment_mod.ApiFootballProvider
    enrichment_mod.ApiFootballProvider = lambda api_key, now: provider
    try:
        with tempfile.TemporaryDirectory() as d:
            cache = FootballCache(db_path=os.path.join(d, "c.db"), now=NOW)
            enrich_matched_candidates(candidates, fixtures_by_event_id, api_key="fake-key", cache=cache, now=NOW)
    finally:
        enrichment_mod.ApiFootballProvider = original_provider_cls

    check("no real data for either team -> statistics_probability stays None, never a guessed 0.5",
          home.statistics_probability is None)
    check("estimated_probability falls back exactly to the market consensus",
          abs(home.estimated_probability - home.consensus_probability) < 1e-9)


def run():
    test_missing_api_key_stays_market_only_and_reports_why()
    test_real_form_data_blends_into_estimated_probability()
    test_team_with_zero_real_matches_never_guessed_as_neutral()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
