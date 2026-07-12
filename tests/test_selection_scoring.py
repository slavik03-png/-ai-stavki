"""
Tests for selection_engine/scoring.py: implied probability, edge/EV, data
completeness, sample reliability, and the confidence-score formula.
"""

import sys

sys.path.insert(0, ".")

from selection_engine.config import ConfidenceWeights, SampleBand, SelectionConfig
from selection_engine.scoring import (
    bookmaker_probability,
    compute_confidence_score,
    compute_data_completeness,
    compute_edge,
    compute_expected_value,
    compute_fair_odds,
    missing_required_fields,
    raw_implied_probability,
    sample_reliability_factor,
)

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def test_raw_implied_probability():
    check("odds 2.0 implies 50%", abs(raw_implied_probability(2.0) - 0.5) < 1e-9)
    check("odds 4.0 implies 25%", abs(raw_implied_probability(4.0) - 0.25) < 1e-9)
    try:
        raw_implied_probability(0)
        check("zero odds rejected", False)
    except ValueError:
        check("zero odds rejected", True)


def test_bookmaker_probability_margin_removal():
    # A 3-way market with obvious overround: 1/1.9 + 1/3.6 + 1/4.2 > 1
    raw, adjusted, is_adj = bookmaker_probability(1.9, complete_market_odds=[1.9, 3.6, 4.2])
    check("margin adjustment flagged", is_adj is True)
    check("margin-adjusted probability lower than raw (overround removed)", adjusted < raw, (raw, adjusted))
    raw2, adjusted2, is_adj2 = bookmaker_probability(2.0)
    check("no market odds means no margin adjustment", is_adj2 is False and adjusted2 is None)


def test_edge_and_ev():
    edge = compute_edge(0.60, 0.50)
    check("edge is model minus bookmaker probability", abs(edge - 0.10) < 1e-9)
    ev = compute_expected_value(0.60, 2.0)
    check("EV = prob*odds - 1", abs(ev - 0.20) < 1e-9)
    ev_negative = compute_expected_value(0.40, 2.0)
    check("EV negative when overpriced by model view", ev_negative < 0, ev_negative)
    check("fair odds is inverse of probability", abs(compute_fair_odds(0.5) - 2.0) < 1e-9)
    check("fair odds undefined at zero probability", compute_fair_odds(0.0) is None)


def test_data_completeness():
    required = ["a", "b"]
    optional = ["c", "d"]
    full = compute_data_completeness({"a": True, "b": True, "c": True, "d": True}, required, optional)
    check("full completeness is 1.0", abs(full - 1.0) < 1e-9, full)

    only_required = compute_data_completeness({"a": True, "b": True}, required, optional)
    # required weight 2 each = 4, optional weight 1 each = 2, total = 6, earned = 4
    check("required-only completeness matches weighted formula", abs(only_required - (4 / 6)) < 1e-9, only_required)

    missing_one_required = compute_data_completeness({"a": True, "c": True, "d": True}, required, optional)
    check(
        "missing a required field lowers completeness below full",
        missing_one_required < full,
        missing_one_required,
    )
    missing_one_required_with_other_required = compute_data_completeness(
        {"a": True, "b": True, "c": True}, required, optional
    )
    missing_two_required = compute_data_completeness({"c": True, "d": True}, required, optional)
    check(
        "missing both required fields scores lower than missing only one",
        missing_two_required < missing_one_required_with_other_required,
        (missing_two_required, missing_one_required_with_other_required),
    )

    missing = missing_required_fields({"a": True}, required)
    check("missing_required_fields reports absent required fields", missing == ["b"], missing)

    empty = compute_data_completeness({}, [], [])
    check("no requirements at all means full completeness", empty == 1.0)


def test_sample_reliability_bands():
    bands = [
        SampleBand(max_matches=5, factor=0.15, label="very low"),
        SampleBand(max_matches=10, factor=0.45, label="low"),
        SampleBand(max_matches=20, factor=0.75, label="moderate"),
        SampleBand(max_matches=10 ** 9, factor=1.0, label="sufficient"),
    ]
    check("tiny sample gets lowest factor", sample_reliability_factor(2, bands) == 0.15)
    check("sample of 25 gets full factor", sample_reliability_factor(25, bands) == 1.0)
    check("sample of exactly 10 rolls into next band (exclusive upper bound)", sample_reliability_factor(10, bands) == 0.75)


def test_confidence_score_formula_bounds_and_monotonicity():
    weights = ConfidenceWeights()
    base_kwargs = dict(
        model_probability=0.70,
        expected_value=0.10,
        data_completeness=0.9,
        sample_reliability=0.9,
        market_reliability=0.7,
        historical_calibration_quality=0.6,
        historical_market_quality=0.6,
        missing_field_count=0,
        is_contradictory=False,
        is_stale=False,
        weights=weights,
    )
    base = compute_confidence_score(**base_kwargs)
    check("confidence score within 0..100 bounds", 0.0 <= base <= 100.0, base)

    worse_ev = dict(base_kwargs)
    worse_ev["expected_value"] = -0.10
    worse = compute_confidence_score(**worse_ev)
    check("lower expected value lowers confidence score", worse < base, (base, worse))

    with_missing = dict(base_kwargs)
    with_missing["missing_field_count"] = 3
    penalised = compute_confidence_score(**with_missing)
    check("missing required fields lower confidence score", penalised < base, (base, penalised))

    contradictory = dict(base_kwargs)
    contradictory["is_contradictory"] = True
    contradicted_score = compute_confidence_score(**contradictory)
    check("contradictory signals lower confidence score", contradicted_score < base, (base, contradicted_score))

    stale = dict(base_kwargs)
    stale["is_stale"] = True
    stale_score = compute_confidence_score(**stale)
    check("stale data lowers confidence score", stale_score < base, (base, stale_score))

    extreme = dict(base_kwargs)
    extreme.update(model_probability=1.0, expected_value=5.0, data_completeness=1.0,
                    sample_reliability=1.0, market_reliability=1.0,
                    historical_calibration_quality=1.0, historical_market_quality=1.0)
    extreme_score = compute_confidence_score(**extreme)
    check("confidence score never exceeds 100 even with extreme inputs", extreme_score <= 100.0, extreme_score)


def run():
    test_raw_implied_probability()
    test_bookmaker_probability_margin_removal()
    test_edge_and_ev()
    test_data_completeness()
    test_sample_reliability_bands()
    test_confidence_score_formula_bounds_and_monotonicity()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
