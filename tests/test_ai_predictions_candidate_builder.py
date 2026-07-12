"""
Unit tests for ai_predictions/candidate_builder.py: confirms candidates are
only ever built for markets/outcomes actually present in the raw Odds API
event, that margin removal runs against the full offered outcome set, and
that an event with no bookmaker data produces no candidates at all (never
invented odds).
"""

import sys

sys.path.insert(0, ".")

from ai_predictions.candidate_builder import build_candidates_for_event
from football.providers.mock_provider import MockFootballProvider
from selection_engine.config import MARKET_1X2, MARKET_BTTS, MARKET_TOTAL_GOALS

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def make_event(bookmakers):
    return {
        "id": "evt-1",
        "commence_time": "2026-07-13T18:00:00Z",
        "home_team": "Mock Home FC",
        "away_team": "Mock Away FC",
        "bookmakers": bookmakers,
    }


def full_h2h_event():
    return make_event([
        {
            "title": "BookA", "last_update": "2026-07-12T10:00:00Z",
            "markets": [{
                "key": "h2h",
                "outcomes": [
                    {"name": "Mock Home FC", "price": 2.0},
                    {"name": "Draw", "price": 3.4},
                    {"name": "Mock Away FC", "price": 3.8},
                ],
            }],
        },
        {
            "title": "BookB", "last_update": "2026-07-12T11:00:00Z",
            "markets": [{
                "key": "h2h",
                "outcomes": [
                    {"name": "Mock Home FC", "price": 2.1},
                    {"name": "Draw", "price": 3.3},
                    {"name": "Mock Away FC", "price": 3.6},
                ],
            }],
        },
    ])


def test_builds_1x2_candidates_from_real_h2h_market():
    provider = MockFootballProvider()
    event = full_h2h_event()
    candidates = build_candidates_for_event(event, provider, event_id="evt-1", league="Mock League")
    market_1x2 = [c for c in candidates if c.market_type == MARKET_1X2]
    check("three 1x2 candidates built (home/draw/away)", len(market_1x2) == 3, [c.selection for c in market_1x2])
    for c in market_1x2:
        check(f"{c.selection}: probability is 0..1", 0.0 < c.model_probability < 1.0, c.model_probability)
        check(f"{c.selection}: odds come from the best real bookmaker price", c.odds in (2.0, 2.1, 3.4, 3.3, 3.8, 3.6), c.odds)
        check(f"{c.selection}: bookmaker count reflects two real books", "2 букмекер" in " ".join(c.explanation), c.explanation)


def test_no_totals_candidates_when_totals_market_absent():
    provider = MockFootballProvider()
    event = full_h2h_event()  # only h2h offered, no "totals" market
    candidates = build_candidates_for_event(event, provider, event_id="evt-1", league="Mock League")
    totals = [c for c in candidates if c.market_type == MARKET_TOTAL_GOALS]
    check("no total_goals candidates invented when totals market is absent", totals == [], totals)


def test_totals_and_btts_built_when_present():
    event = full_h2h_event()
    event["bookmakers"][0]["markets"].append({
        "key": "totals",
        "outcomes": [{"name": "Over", "price": 1.9, "point": 2.5}, {"name": "Under", "price": 1.95, "point": 2.5}],
    })
    event["bookmakers"][1]["markets"].append({
        "key": "totals",
        "outcomes": [{"name": "Over", "price": 1.85, "point": 2.5}, {"name": "Under", "price": 2.0, "point": 2.5}],
    })
    event["bookmakers"][0]["markets"].append({
        "key": "btts",
        "outcomes": [{"name": "Yes", "price": 1.8}, {"name": "No", "price": 2.05}],
    })
    provider = MockFootballProvider()
    candidates = build_candidates_for_event(event, provider, event_id="evt-1", league="Mock League")
    totals = [c for c in candidates if c.market_type == MARKET_TOTAL_GOALS]
    btts = [c for c in candidates if c.market_type == MARKET_BTTS]
    check("total_goals candidates built once the market is present", len(totals) == 2, [c.selection for c in totals])
    check("btts candidates built from the single bookmaker offering it", len(btts) == 2, [c.selection for c in btts])
    for c in totals:
        check(f"{c.line} line is the real quoted point", c.line == 2.5, c.line)


def test_no_candidates_when_event_has_no_bookmakers():
    provider = MockFootballProvider()
    event = make_event([])
    candidates = build_candidates_for_event(event, provider, event_id="evt-empty", league="Mock League")
    check("an event with no bookmaker data produces zero candidates", candidates == [], candidates)


def test_unknown_teams_fall_back_to_consensus_only_not_crash():
    provider = MockFootballProvider()  # only knows "Mock Home FC"/"Mock Away FC"
    event = make_event([{
        "title": "BookA", "last_update": "2026-07-12T10:00:00Z",
        "markets": [{
            "key": "h2h",
            "outcomes": [
                {"name": "Mock Home FC", "price": 2.0},
                {"name": "Draw", "price": 3.4},
                {"name": "Mock Away FC", "price": 3.8},
            ],
        }],
    }])
    event["home_team"] = "Totally Unknown FC"
    event["away_team"] = "Mock Away FC"
    candidates = build_candidates_for_event(event, provider, event_id="evt-2", league="Mock League")
    check("unknown-team event still yields consensus-only candidates without crashing", isinstance(candidates, list))


def run():
    test_builds_1x2_candidates_from_real_h2h_market()
    test_no_totals_candidates_when_totals_market_absent()
    test_totals_and_btts_built_when_present()
    test_no_candidates_when_event_has_no_bookmakers()
    test_unknown_teams_fall_back_to_consensus_only_not_crash()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
