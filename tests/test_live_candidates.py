"""
Unit tests for ai_predictions/live_candidates.py -- Live mode's
cross-bookmaker consensus candidate builder (Task #11), reusing
ai_predictions/value_engine.py's real math.
"""

import datetime
import sys

sys.path.insert(0, ".")

from ai_predictions.fixture_matching import match_fixtures_to_events
from ai_predictions.live_candidates import build_live_candidates
from ai_predictions.live_fixtures import LiveFixture
from ai_predictions.value_config import SIGNAL_REJECTED

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def _live_fixture(fixture_id=1, home="Home FC", away="Away FC"):
    return LiveFixture(
        fixture_id=fixture_id,
        kickoff_utc=datetime.datetime(2026, 7, 15, 11, 0, tzinfo=datetime.timezone.utc),
        home_team=home, away_team=away,
        league_name="Test League", league_country="Testland",
        status_short="2H", elapsed_minutes=67, home_score=1, away_score=0,
    )


def _event(home="Home FC", away="Away FC", prices=None):
    prices = prices or [
        ("BookA", 2.00, 3.30, 4.00),
        ("BookB", 1.98, 3.35, 4.05),
        ("BookC", 2.02, 3.25, 3.95),
        ("BookD", 2.30, 3.15, 3.65),  # clear best price on Home
    ]
    return {
        "id": "evt-live-1",
        "_sport_key": "soccer_epl",
        "home_team": home,
        "away_team": away,
        "commence_time": "2026-07-15T11:00:00Z",
        "bookmakers": [
            {
                "title": title,
                "last_update": "2026-07-15T12:30:00Z",
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


def test_matched_fixture_with_real_edge_produces_a_candidate():
    fixture = _live_fixture()
    match_result = match_fixtures_to_events([fixture], [_event()])
    check("fixture matched to the real event", len(match_result.matches) == 1)
    live_candidates = build_live_candidates(match_result.matches)
    check("one live candidate produced", len(live_candidates) == 1)
    lc = live_candidates[0]
    check("candidate carries the live fixture (minute/score)", lc.live_fixture.elapsed_minutes == 67)
    check("candidate's signal level is never REJECTED", lc.value_candidate.signal_level != SIGNAL_REJECTED)
    check("candidate has a real best_price/bookmaker", lc.value_candidate.best_price > 0 and lc.value_candidate.best_bookmaker)


def test_unmatched_fixture_produces_no_candidate():
    fixture = _live_fixture(fixture_id=2, home="Nobody FC", away="Nowhere FC")
    # An event for a completely different match -- team names too dissimilar to match.
    match_result = match_fixtures_to_events([fixture], [_event(home="Other Team", away="Another Team")])
    check("dissimilar teams are never matched", match_result.matches == [])
    live_candidates = build_live_candidates(match_result.matches)
    check("no matched fixture -> no live candidates at all", live_candidates == [])


def test_flat_market_with_no_edge_is_dropped_not_shown():
    fixture = _live_fixture(fixture_id=3)
    flat_prices = [
        ("BookA", 2.00, 3.30, 4.00),
        ("BookB", 2.00, 3.30, 4.00),
        ("BookC", 2.00, 3.30, 4.00),
        ("BookD", 2.00, 3.30, 4.00),
    ]
    match_result = match_fixtures_to_events([fixture], [_event(prices=flat_prices)])
    live_candidates = build_live_candidates(match_result.matches)
    check("identical prices across bookmakers -> no real edge -> dropped, never shown weak", live_candidates == [])


def run():
    test_matched_fixture_with_real_edge_produces_a_candidate()
    test_unmatched_fixture_produces_no_candidate()
    test_flat_market_with_no_edge_is_dropped_not_shown()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
