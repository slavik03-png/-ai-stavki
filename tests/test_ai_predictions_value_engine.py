"""
Unit tests for ai_predictions/value_engine.py: cross-bookmaker
price-divergence detection using only synthetic-but-realistic bookmaker
JSON (no network calls, no football statistics provider involved).
"""

import sys

sys.path.insert(0, ".")

from ai_predictions.value_engine import (
    MIN_BOOKMAKERS,
    build_value_candidates_for_event,
)

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def _event_with_bookmakers(prices):
    """prices: list of (title, home_price, draw_price, away_price)."""
    return {
        "id": "evt-1",
        "home_team": "Home FC",
        "away_team": "Away FC",
        "commence_time": "2026-07-13T12:00:00Z",
        "bookmakers": [
            {
                "title": title,
                "last_update": "2026-07-12T10:00:00Z",
                "markets": [{
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Home FC", "price": h},
                        {"name": "Draw", "price": d},
                        {"name": "Away FC", "price": a},
                    ],
                }],
            }
            for title, h, d, a in prices
        ],
    }


def test_no_bookmakers_yields_no_candidates():
    event = _event_with_bookmakers([])
    candidates = build_value_candidates_for_event(event, event_id="evt-1", league="Test League")
    check("no bookmakers -> no candidates at all", candidates == [], candidates)


def test_below_min_bookmakers_is_rejected_but_not_invented():
    # Only 2 bookmakers < MIN_BOOKMAKERS -- must be rejected, not hidden or padded.
    event = _event_with_bookmakers([
        ("BookA", 2.00, 3.30, 4.00),
        ("BookB", 2.00, 3.30, 4.00),
    ])
    candidates = build_value_candidates_for_event(event, event_id="evt-1", league="Test League")
    home_candidates = [c for c in candidates if c.selection == "Home FC"]
    check("candidate is still built (real data) even below the bookmaker minimum", len(home_candidates) == 1)
    check("but is marked rejected for too few bookmakers",
          any("букмекер" in r for r in home_candidates[0].rejection_reasons), home_candidates[0].rejection_reasons)
    check("MIN_BOOKMAKERS constant matches the spec (>= 3)", MIN_BOOKMAKERS == 3)


def test_genuine_divergence_is_detected_and_passes():
    # 4 bookmakers agree on ~2.00 for Home FC, one outlier offers 2.30 --
    # a real, structural price divergence a value bettor would want to see.
    event = _event_with_bookmakers([
        ("BookA", 2.00, 3.30, 4.00),
        ("BookB", 1.98, 3.35, 4.05),
        ("BookC", 2.02, 3.25, 3.95),
        ("BookD", 2.30, 3.10, 3.60),  # outlier, best price
    ])
    candidates = build_value_candidates_for_event(event, event_id="evt-1", league="Test League")
    home = next(c for c in candidates if c.selection == "Home FC")
    check("best price is the real outlier price", home.best_price == 2.30, home.best_price)
    check("best bookmaker is correctly identified", home.best_bookmaker == "BookD")
    check("consensus excludes the best-price bookmaker itself (leave-one-out)", home.consensus_bookmaker_count == 3)
    check("edge is positive (best price beats the real consensus)", home.edge > 0, home.edge)
    check("candidate with real divergence passes all thresholds", home.passed, home.rejection_reasons)


def test_flat_market_has_near_zero_edge_and_is_rejected():
    # All bookmakers agree closely -- no real divergence, must not be recommended.
    event = _event_with_bookmakers([
        ("BookA", 2.00, 3.30, 4.00),
        ("BookB", 2.01, 3.29, 3.99),
        ("BookC", 1.99, 3.31, 4.01),
        ("BookD", 2.00, 3.30, 4.00),
    ])
    candidates = build_value_candidates_for_event(event, event_id="evt-1", league="Test League")
    home = next(c for c in candidates if c.selection == "Home FC")
    check("flat market produces near-zero edge", abs(home.edge) < 0.03, home.edge)
    check("flat market candidate is rejected, not recommended", not home.passed, home.rejection_reasons)


def test_only_real_selections_offered_by_bookmakers_are_built():
    # No totals market offered at all -- must not invent a totals candidate.
    event = _event_with_bookmakers([
        ("BookA", 2.00, 3.30, 4.00),
        ("BookB", 2.00, 3.30, 4.00),
        ("BookC", 2.00, 3.30, 4.00),
    ])
    candidates = build_value_candidates_for_event(event, event_id="evt-1", league="Test League")
    check("no totals candidates invented when no bookmaker offered totals",
          all(c.market_type != "total_goals" for c in candidates), [c.market_type for c in candidates])


def run():
    test_no_bookmakers_yields_no_candidates()
    test_below_min_bookmakers_is_rejected_but_not_invented()
    test_genuine_divergence_is_detected_and_passes()
    test_flat_market_has_near_zero_edge_and_is_rejected()
    test_only_real_selections_offered_by_bookmakers_are_built()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
