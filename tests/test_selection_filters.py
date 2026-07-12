"""
Tests for selection_engine/filters.py: minimum filter rejection rules.
"""

import datetime
import sys

sys.path.insert(0, ".")

from selection_engine.config import SelectionConfig
from selection_engine.filters import apply_filters, filter_candidates
from selection_engine.models import CandidatePrediction

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


NOW = datetime.datetime(2026, 7, 12, 12, 0, tzinfo=datetime.timezone.utc)
FUTURE = "2026-07-12T20:00:00+00:00"
PAST = "2026-07-12T06:00:00+00:00"


def make_candidate(**overrides):
    base = dict(
        event_id="ev1", sport="football", league="Premier League", country="England",
        match_datetime=FUTURE, home_team="A", away_team="B",
        market_type="1x2", selection="1", line=None, bookmaker="BookX",
        odds=2.0, model_probability=0.6,
        available_fields={"home_form": True, "away_form": True, "sample_size": True},
        sample_size=25,
    )
    base.update(overrides)
    c = CandidatePrediction(**base)
    c.edge = 0.10
    c.expected_value = 0.10
    c.confidence_score = 80.0
    c.data_completeness = 0.9
    return c


def test_valid_candidate_passes():
    c = make_candidate()
    ok, reasons = apply_filters(c, SelectionConfig(), now=NOW, seen_dedup_keys=set())
    check("a fully valid candidate passes all filters", ok, reasons)


def test_rejects_bad_odds():
    c = make_candidate(odds=1.0)
    ok, reasons = apply_filters(c, SelectionConfig(), now=NOW, seen_dedup_keys=set())
    check("odds of exactly 1.0 rejected", not ok and any("оэффициент" in r for r in reasons), reasons)

    c2 = make_candidate(odds=1.10)
    ok2, reasons2 = apply_filters(c2, SelectionConfig(), now=NOW, seen_dedup_keys=set())
    check("odds below minimum threshold rejected", not ok2, reasons2)


def test_rejects_negative_ev():
    c = make_candidate()
    c.expected_value = -0.05
    ok, reasons = apply_filters(c, SelectionConfig(), now=NOW, seen_dedup_keys=set())
    check("negative expected value rejected", not ok, reasons)


def test_rejects_low_confidence():
    c = make_candidate()
    c.confidence_score = 50.0
    ok, reasons = apply_filters(c, SelectionConfig(), now=NOW, seen_dedup_keys=set())
    check("confidence below minimum rejected", not ok, reasons)


def test_rejects_low_data_completeness():
    c = make_candidate()
    c.data_completeness = 0.4
    ok, reasons = apply_filters(c, SelectionConfig(), now=NOW, seen_dedup_keys=set())
    check("low data completeness rejected", not ok, reasons)


def test_rejects_missing_required_fields():
    c = make_candidate(available_fields={"home_form": True})
    ok, reasons = apply_filters(c, SelectionConfig(), now=NOW, seen_dedup_keys=set())
    check("missing required market fields rejected", not ok, reasons)


def test_rejects_started_event():
    c = make_candidate(match_datetime=PAST)
    ok, reasons = apply_filters(c, SelectionConfig(), now=NOW, seen_dedup_keys=set())
    check("event that already started is rejected", not ok, reasons)


def test_rejects_stale_price():
    c = make_candidate()
    c.price_timestamp = "2026-07-12T10:00:00+00:00"  # 2 hours before NOW
    ok, reasons = apply_filters(c, SelectionConfig(), now=NOW, seen_dedup_keys=set())
    check("stale price beyond max age rejected", not ok, reasons)


def test_rejects_duplicate_within_batch():
    c1 = make_candidate()
    c2 = make_candidate()
    passed, rejected = filter_candidates([c1, c2], SelectionConfig(), now=NOW)
    check("first duplicate passes, second is rejected", len(passed) == 1 and len(rejected) == 1)
    check("duplicate rejection reason mentions duplication", any("ублир" in r for r in rejected[0].rejection_reasons))


def test_rejects_suspended_market():
    c = make_candidate()
    ok, reasons = apply_filters(
        c, SelectionConfig(), now=NOW, seen_dedup_keys=set(), is_market_suspended=True
    )
    check("suspended market rejected", not ok, reasons)


def test_rejects_unknown_market_type():
    c = make_candidate(market_type="unknown_market")
    ok, reasons = apply_filters(c, SelectionConfig(), now=NOW, seen_dedup_keys=set())
    check("unsupported/unsettleable market type rejected", not ok, reasons)


def test_rejects_disabled_market():
    config = SelectionConfig(disabled_markets=frozenset({"1x2"}))
    c = make_candidate()
    ok, reasons = apply_filters(c, config, now=NOW, seen_dedup_keys=set())
    check("explicitly disabled market rejected", not ok, reasons)


def run():
    test_valid_candidate_passes()
    test_rejects_bad_odds()
    test_rejects_negative_ev()
    test_rejects_low_confidence()
    test_rejects_low_data_completeness()
    test_rejects_missing_required_fields()
    test_rejects_started_event()
    test_rejects_stale_price()
    test_rejects_duplicate_within_batch()
    test_rejects_suspended_market()
    test_rejects_unknown_market_type()
    test_rejects_disabled_market()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
