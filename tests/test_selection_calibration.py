"""
Tests for selection_engine/calibration.py: bucketing settled predictions by
confidence, computing calibrated probability, and staying conservative on
thin samples.
"""

import sys

sys.path.insert(0, ".")

from selection_engine.config import SelectionConfig
from selection_engine.calibration import build_calibration_buckets, calibrate_probability

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


class FakeRow(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def _row(status, confidence_score, model_probability):
    return FakeRow(status=status, confidence_score=confidence_score, model_probability=model_probability)


def test_excludes_non_decisive_results():
    rows = [
        _row("won", 82, 0.65),
        _row("pending", 82, 0.65),
        _row("returned", 82, 0.65),
        _row("cancelled", 82, 0.65),
    ]
    config = SelectionConfig(calibration_min_sample=1)
    buckets = build_calibration_buckets(rows, config)
    bucket = buckets["80-90"]
    check("only the decisive (won) row counts toward the bucket", bucket.settled_decisive_count == 1, bucket.settled_decisive_count)


def test_preliminary_flag_below_minimum_sample():
    rows = [_row("won", 82, 0.65) for _ in range(5)]
    config = SelectionConfig(calibration_min_sample=20)
    buckets = build_calibration_buckets(rows, config)
    bucket = buckets["80-90"]
    check("bucket below minimum sample is marked preliminary", bucket.is_preliminary)

    result = calibrate_probability(0.65, 82, buckets, config)
    check("preliminary bucket leaves probability unchanged", abs(result.calibrated_probability - 0.65) < 1e-9)
    check("preliminary flag propagates to the result", result.is_preliminary)


def test_calibration_adjusts_with_sufficient_sample():
    # Model predicted ~0.65 on average but actual success rate was only 45%
    # -- a well-sampled overconfidence should nudge the probability down.
    rows = []
    for i in range(50):
        status = "won" if i < 22 else "lost"  # 22/50 = 44% actual success
        rows.append(_row(status, 82, 0.65))
    config = SelectionConfig(calibration_min_sample=20)
    buckets = build_calibration_buckets(rows, config)
    bucket = buckets["80-90"]
    check("large sample bucket is not preliminary", not bucket.is_preliminary)

    result = calibrate_probability(0.65, 82, buckets, config)
    check("overconfident bucket calibrates probability downward", result.calibrated_probability < 0.65, result.calibrated_probability)
    check(
        "calibration adjustment never exceeds configured max",
        abs(result.calibrated_probability - 0.65) <= config.calibration_max_adjustment + 1e-9,
        result.calibrated_probability,
    )


def test_never_uses_unresolved_pending_results():
    rows = [_row("pending", 82, 0.65), _row("unresolved", 82, 0.65), _row("postponed", 82, 0.65)]
    config = SelectionConfig(calibration_min_sample=1)
    buckets = build_calibration_buckets(rows, config)
    bucket = buckets["80-90"]
    check("pending/unresolved/postponed rows never contribute to calibration", bucket.settled_decisive_count == 0)


def run():
    test_excludes_non_decisive_results()
    test_preliminary_flag_below_minimum_sample()
    test_calibration_adjusts_with_sufficient_sample()
    test_never_uses_unresolved_pending_results()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
