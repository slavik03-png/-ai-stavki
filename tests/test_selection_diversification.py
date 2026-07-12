"""
Tests for selection_engine/diversification.py: correlation groups, one
pick per event, and anti-concentration by league/market family.
"""

import datetime
import sys

sys.path.insert(0, ".")

from selection_engine.config import SelectionConfig
from selection_engine.diversification import diversify
from selection_engine.models import CandidatePrediction

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


FUTURE = "2026-07-12T20:00:00+00:00"


def make(event_id, market_type, selection_score, league="Premier League", selection="1"):
    c = CandidatePrediction(
        event_id=event_id, sport="football", league=league, country="England",
        match_datetime=FUTURE, home_team=f"Home-{event_id}", away_team=f"Away-{event_id}",
        market_type=market_type, selection=selection, line=None, bookmaker="BookX",
        odds=2.0, model_probability=0.6,
        available_fields={}, sample_size=25,
    )
    c.selection_score = selection_score
    return c


def test_one_main_per_event():
    c1 = make("ev1", "1x2", 0.90, selection="1")
    c2 = make("ev1", "btts", 0.85, selection="yes")
    kept, dropped = diversify([c1, c2], SelectionConfig(), max_picks=5)
    check("only the strongest pick per event is kept", len(kept) == 1 and kept[0] is c1)
    check("the weaker same-event pick is dropped for diversity, not discarded entirely", dropped[0] is c2)


def test_correlated_family_dedup_same_event():
    c1 = make("ev1", "1x2", 0.90)
    c2 = make("ev1", "double_chance", 0.85)  # same correlation family: match_result
    kept, dropped = diversify([c1, c2], SelectionConfig(), max_picks=5)
    check("correlated markets on the same event are deduplicated", len(kept) == 1)


def test_independent_events_both_kept():
    c1 = make("ev1", "1x2", 0.90)
    c2 = make("ev2", "1x2", 0.85)
    kept, dropped = diversify([c1, c2], SelectionConfig(), max_picks=5)
    check("independent events are not deduplicated against each other", len(kept) == 2)


def test_max_picks_respected():
    config = SelectionConfig(max_main_per_league=10, max_main_per_market_family=10)
    candidates = [
        make(f"ev{i}", "1x2", 1.0 - i * 0.01, league=f"League-{i}") for i in range(10)
    ]
    kept, dropped = diversify(candidates, config, max_picks=3)
    check("diversify never exceeds max_picks", len(kept) == 3, len(kept))
    check("excess candidates land in dropped, not discarded", len(dropped) == 7)


def test_league_concentration_cap():
    config = SelectionConfig(max_main_per_league=2, max_main_per_market_family=10)
    candidates = [make(f"ev{i}", "corners_total", 1.0 - i * 0.01, league="Serie A") for i in range(5)]
    kept, dropped = diversify(candidates, config, max_picks=10)
    check("league concentration cap is enforced", len(kept) == 2, len(kept))


def test_market_family_concentration_cap():
    config = SelectionConfig(max_main_per_league=10, max_main_per_market_family=1)
    candidates = [
        make("ev1", "corners_total", 0.9, league="A"),
        make("ev2", "corners_total", 0.85, league="B"),
    ]
    kept, dropped = diversify(candidates, config, max_picks=10)
    check("market family concentration cap is enforced across different events", len(kept) == 1)


def test_no_padding_when_fewer_candidates_than_max():
    c1 = make("ev1", "1x2", 0.9)
    kept, dropped = diversify([c1], SelectionConfig(), max_picks=8)
    check("diversify never invents extra picks to fill the quota", len(kept) == 1 and len(dropped) == 0)


def run():
    test_one_main_per_event()
    test_correlated_family_dedup_same_event()
    test_independent_events_both_kept()
    test_max_picks_respected()
    test_league_concentration_cap()
    test_market_family_concentration_cap()
    test_no_padding_when_fewer_candidates_than_max()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
