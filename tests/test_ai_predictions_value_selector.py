"""
Unit tests for ai_predictions/value_selector.py: per-level caps, ranking
order, one-signal-per-event rules and the multi-market exception (Step 6).
Builds ValueCandidate objects directly rather than through the odds JSON
extraction pipeline, since selection logic is independent of how a
candidate's signal_level/ranking_score were computed.
"""

import sys

sys.path.insert(0, ".")

from ai_predictions.value_config import MAX_SIGNALS_PER_LEVEL, SIGNAL_HIGH, SIGNAL_LOW, SIGNAL_MEDIUM, SIGNAL_REJECTED
from ai_predictions.value_engine import ValueCandidate
from ai_predictions.value_selector import select_value_recommendations

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def _candidate(event_id, market_type, selection, level, score, **overrides):
    base = dict(
        event_id=event_id, sport="soccer", league="Test League", country=None,
        match_datetime="2026-07-13T12:00:00Z", home_team="Home FC", away_team="Away FC",
        market_type=market_type, selection=selection, line=None,
        best_bookmaker="BookX", best_price=2.0, best_price_implied_probability=0.45,
        consensus_probability=0.5, consensus_bookmaker_count=3, fair_price=2.0,
        edge=0.05, expected_value=0.08, bookmaker_count=4, all_prices=[2.0, 2.0, 2.0, 2.0],
        unique_bookmaker_count=4, signal_level=level, ranking_score=score,
    )
    base.update(overrides)
    return ValueCandidate(**base)


def test_caps_at_max_signals_per_level():
    candidates = [
        _candidate(f"evt-{i}", "1x2", "Home FC", SIGNAL_HIGH, score=float(i))
        for i in range(MAX_SIGNALS_PER_LEVEL + 3)
    ]
    result = select_value_recommendations(candidates)
    check(f"never more than {MAX_SIGNALS_PER_LEVEL} HIGH signals shown",
          len(result.high) == MAX_SIGNALS_PER_LEVEL, len(result.high))
    check("overflow candidates land in rejected, not silently dropped",
          len(result.rejected) == 3, len(result.rejected))


def test_ranked_by_score_not_insertion_order():
    candidates = [
        _candidate("evt-a", "1x2", "Home FC", SIGNAL_HIGH, score=1.0),
        _candidate("evt-b", "1x2", "Home FC", SIGNAL_HIGH, score=9.0),
        _candidate("evt-c", "1x2", "Home FC", SIGNAL_HIGH, score=5.0),
    ]
    result = select_value_recommendations(candidates)
    check("signals are ordered by ranking_score, highest first",
          [c.event_id for c in result.high] == ["evt-b", "evt-c", "evt-a"], [c.event_id for c in result.high])


def test_one_signal_per_event_same_market():
    candidates = [
        _candidate("evt-1", "1x2", "Home FC", SIGNAL_HIGH, score=5.0),
        _candidate("evt-1", "1x2", "Away FC", SIGNAL_HIGH, score=9.0),
    ]
    result = select_value_recommendations(candidates)
    check("only the stronger of two same-market signals on one event is shown",
          len(result.high) == 1 and result.high[0].selection == "Away FC", [c.selection for c in result.high])
    check("the weaker duplicate is recorded as rejected, not discarded silently",
          len(result.rejected) == 1)


def test_different_market_both_strong_gets_two_slots():
    candidates = [
        _candidate("evt-1", "1x2", "Home FC", SIGNAL_HIGH, score=9.0),
        _candidate("evt-1", "total_goals", "Over 2.5", SIGNAL_MEDIUM, score=6.0),
    ]
    result = select_value_recommendations(candidates)
    check("a HIGH 1x2 signal and a MEDIUM totals signal on the same event both survive",
          len(result.high) + len(result.medium) == 2)


def test_different_market_one_low_does_not_get_a_second_slot():
    candidates = [
        _candidate("evt-1", "1x2", "Home FC", SIGNAL_HIGH, score=9.0),
        _candidate("evt-1", "total_goals", "Over 2.5", SIGNAL_LOW, score=6.0),
    ]
    result = select_value_recommendations(candidates)
    check("a LOW signal never gets a second slot on an event that already has a HIGH signal",
          len(result.high) == 1 and len(result.low) == 0, (len(result.high), len(result.low)))


def test_rejected_candidates_are_never_shown():
    candidates = [
        _candidate("evt-1", "1x2", "Home FC", SIGNAL_REJECTED, score=0.0),
    ]
    result = select_value_recommendations(candidates)
    check("a REJECTED candidate never appears in high/medium/low",
          not result.high and not result.medium and not result.low)
    check("a REJECTED candidate is tracked in the rejected bucket",
          len(result.rejected) == 1)


def test_main_alias_orders_high_then_medium_then_low():
    candidates = [
        _candidate("evt-1", "1x2", "Home FC", SIGNAL_LOW, score=1.0),
        _candidate("evt-2", "1x2", "Home FC", SIGNAL_HIGH, score=1.0),
        _candidate("evt-3", "1x2", "Home FC", SIGNAL_MEDIUM, score=1.0),
    ]
    result = select_value_recommendations(candidates)
    check("backward-compatible .main property orders HIGH, then MEDIUM, then LOW",
          [c.signal_level for c in result.main] == [SIGNAL_HIGH, SIGNAL_MEDIUM, SIGNAL_LOW],
          [c.signal_level for c in result.main])


def run():
    test_caps_at_max_signals_per_level()
    test_ranked_by_score_not_insertion_order()
    test_one_signal_per_event_same_market()
    test_different_market_both_strong_gets_two_slots()
    test_different_market_one_low_does_not_get_a_second_slot()
    test_rejected_candidates_are_never_shown()
    test_main_alias_orders_high_then_medium_then_low()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
