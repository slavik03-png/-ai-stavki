"""
Unit tests for ai_predictions/probability_model.py: the auditable market +
statistics probability blend. Pure functions, no network/randomness.
"""

import sys

sys.path.insert(0, ".")

from ai_predictions.probability_model import (
    blend_probability,
    sample_size_category,
    statistics_probability_for_side,
)

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def test_sample_size_category_capped_by_weaker_side():
    check("both strong -> strong", sample_size_category(10, 12) == "strong")
    check("one weak side caps the whole category", sample_size_category(10, 1) == "weak")
    check("both zero -> none", sample_size_category(0, 0) == "none")
    check("medium threshold respected", sample_size_category(4, 4) == "medium")


def test_statistics_probability_missing_data_never_guessed():
    check("None inputs -> None, never a neutral guess", statistics_probability_for_side(None, None, "home") is None)
    check("zero-zero win rates -> None, not divide-by-zero 0.5", statistics_probability_for_side(0.0, 0.0, "home") is None)


def test_statistics_probability_for_side_normalizes_correctly():
    prob_home = statistics_probability_for_side(0.6, 0.3, "home")
    prob_away = statistics_probability_for_side(0.6, 0.3, "away")
    check("home share computed correctly", abs(prob_home - (0.6 / 0.9)) < 1e-9, prob_home)
    check("home+away shares sum to 1", abs(prob_home + prob_away - 1.0) < 1e-9)


def test_blend_falls_back_to_market_only_when_no_statistics():
    result = blend_probability(0.55, None, 0, 0)
    check("no statistics -> pure market probability", abs(result.estimated_probability - 0.55) < 1e-9, result.estimated_probability)
    check("category recorded as none", result.sample_size_category == "none")
    check("statistics weight is zero", result.statistics_weight == 0.0)


def test_blend_weights_statistics_more_with_a_strong_sample():
    weak_result = blend_probability(0.50, 0.90, 1, 1)
    strong_result = blend_probability(0.50, 0.90, 10, 10)
    check("a strong sample pulls the estimate further toward statistics than a weak sample",
          strong_result.estimated_probability > weak_result.estimated_probability,
          (weak_result.estimated_probability, strong_result.estimated_probability))
    check("weak-sample category recorded", weak_result.sample_size_category == "weak")
    check("strong-sample category recorded", strong_result.sample_size_category == "strong")


def test_blend_never_produces_a_forced_extreme():
    result = blend_probability(0.99, 0.99, 20, 20)
    check("clamped below the hard ceiling even with two confident agreeing inputs", result.estimated_probability <= 0.98)
    result2 = blend_probability(0.01, 0.01, 20, 20)
    check("clamped above the hard floor", result2.estimated_probability >= 0.02)


def run():
    test_sample_size_category_capped_by_weaker_side()
    test_statistics_probability_missing_data_never_guessed()
    test_statistics_probability_for_side_normalizes_correctly()
    test_blend_falls_back_to_market_only_when_no_statistics()
    test_blend_weights_statistics_more_with_a_strong_sample()
    test_blend_never_produces_a_forced_extreme()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
