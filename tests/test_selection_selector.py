"""
End-to-end tests for selection_engine/selector.py: the full pipeline, its
caps, its "never pad weak picks" guarantee, and correct group assignment.
"""

import datetime
import sys

sys.path.insert(0, ".")

from selection_engine.config import SelectionConfig
from selection_engine.models import (
    GROUP_AVOID,
    GROUP_HIGH_RISK,
    GROUP_MAIN,
    GROUP_RESERVE,
    CandidatePrediction,
)
from selection_engine.selector import select_recommendations

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


NOW = datetime.datetime(2026, 7, 12, 12, 0, tzinfo=datetime.timezone.utc)
FUTURE = "2026-07-12T20:00:00+00:00"

FULL_FIELDS_1X2 = {
    "home_form": True, "away_form": True, "sample_size": True,
    "h2h": True, "league_position": True, "injuries": True, "lineups": True,
}


def make(event_id, odds, prob, market_type="1x2", selection="1", sample_size=30,
         league="Premier League", home_team=None, away_team=None, available_fields=None):
    return CandidatePrediction(
        event_id=event_id, sport="football", league=league, country="England",
        match_datetime=FUTURE, home_team=home_team or f"Home-{event_id}", away_team=away_team or f"Away-{event_id}",
        market_type=market_type, selection=selection, line=None, bookmaker="BookX",
        odds=odds, model_probability=prob,
        available_fields=available_fields if available_fields is not None else dict(FULL_FIELDS_1X2),
        sample_size=sample_size,
    )


def strong_candidate(event_id, league="Premier League"):
    return make(event_id, 2.4, 0.84, league=league)


def weak_candidate(event_id):
    c = make(event_id, 2.0, 0.55)
    return c


def test_empty_input_returns_no_recommendation_result():
    result = select_recommendations([], SelectionConfig(), now=NOW)
    check("zero candidates yields zero main recommendations", len(result.main) == 0)
    check("zero candidates still returns explicit reasons", len(result.no_recommendation_reasons) > 0)


def test_weak_candidates_are_never_padded_into_main():
    candidates = [weak_candidate(f"ev{i}") for i in range(5)]
    result = select_recommendations(candidates, SelectionConfig(), now=NOW)
    check("weak candidates never get forced into MAIN to fill quota", len(result.main) == 0, len(result.main))


def test_strong_candidates_reach_main():
    # Distinct market families so the default anti-concentration cap
    # (max_main_per_market_family=2) does not itself limit this case --
    # that cap is tested separately in test_selection_diversification.py.
    c1 = strong_candidate("ev0", league="League-0")
    c2 = make(
        "ev1", 2.3, 0.85, market_type="corners_total", selection="over_9",
        available_fields={"corners": True, "sample_size": True, "current_price": True},
        league="League-1",
    )
    c3 = make(
        "ev2", 2.2, 0.86, market_type="cards_total", selection="over_4",
        available_fields={"cards": True, "sample_size": True, "current_price": True, "lineups": True},
        league="League-2",
    )
    result = select_recommendations([c1, c2, c3], SelectionConfig(), now=NOW)
    check("strong, well-supported candidates reach MAIN", len(result.main) == 3, len(result.main))
    for c in result.main:
        check(f"MAIN candidate {c.event_id} tagged with MAIN group", c.recommendation_group == GROUP_MAIN)


def test_max_five_main_cap():
    candidates = [strong_candidate(f"ev{i}", league=f"League-{i}") for i in range(9)]
    result = select_recommendations(candidates, SelectionConfig(), now=NOW)
    check("MAIN never exceeds the configured cap of 5", len(result.main) <= 5, len(result.main))


def test_fewer_than_five_allowed():
    candidates = [strong_candidate("ev1", league="League-1"), strong_candidate("ev2", league="League-2")]
    result = select_recommendations(candidates, SelectionConfig(), now=NOW)
    check("returning fewer than 5 MAIN picks is allowed when only 2 qualify", len(result.main) == 2, len(result.main))


def test_reserve_cap_of_three():
    candidates = [strong_candidate(f"ev{i}", league=f"League-{i}") for i in range(15)]
    result = select_recommendations(candidates, SelectionConfig(), now=NOW)
    check("RESERVE never exceeds the configured cap of 3", len(result.reserve) <= 3, len(result.reserve))


def test_one_main_per_event():
    c1 = make("ev1", 2.4, 0.84, market_type="1x2", selection="1")
    c2 = make("ev1", 2.2, 0.83, market_type="btts", selection="yes",
              available_fields={"btts_frequency_home": True, "btts_frequency_away": True, "sample_size": True,
                                 "clean_sheets_home": True, "clean_sheets_away": True, "goals_scored_conceded": True})
    result = select_recommendations([c1, c2], SelectionConfig(), now=NOW)
    main_events = {c.event_id for c in result.main}
    check("at most one MAIN pick is produced per event", len(result.main) <= 1, len(result.main))


def test_negative_ev_rejected_even_with_high_probability():
    # High model probability but odds so short the EV is negative -- must
    # not be recommended just because "probability looks high".
    c = make("ev1", 1.10, 0.85)
    result = select_recommendations([c], SelectionConfig(), now=NOW)
    check("negative EV candidate never becomes a recommendation", len(result.main) == 0 and len(result.reserve) == 0)
    check("negative EV candidate lands in AVOID/rejected, not silently dropped",
          any(c.event_id == "ev1" for c in result.avoid) or any(r.event_id == "ev1" for r in result.rejected))


def test_correct_score_is_high_risk_by_default():
    c = make(
        "ev1", 8.0, 0.20, market_type="correct_score", selection="2:1",
        available_fields={"goals_scored_conceded": True, "recent_matches": True, "sample_size": True, "h2h": True, "lineups": True},
    )
    result = select_recommendations([c], SelectionConfig(), now=NOW)
    check("correct_score never lands in MAIN by default", len(result.main) == 0)
    if result.high_risk:
        check("correct_score candidate routed to HIGH_RISK", result.high_risk[0].market_type == "correct_score")


def test_deterministic_output():
    candidates_a = [strong_candidate(f"ev{i}", league=f"League-{i}") for i in range(4)]
    candidates_b = [strong_candidate(f"ev{i}", league=f"League-{i}") for i in range(4)]
    result_a = select_recommendations(candidates_a, SelectionConfig(), now=NOW)
    result_b = select_recommendations(candidates_b, SelectionConfig(), now=NOW)
    ids_a = [c.event_id for c in result_a.main]
    ids_b = [c.event_id for c in result_b.main]
    check("running the same input twice produces the same MAIN selection", ids_a == ids_b, (ids_a, ids_b))


def test_missing_required_market_data_excludes_candidate():
    c = make("ev1", 2.4, 0.84, available_fields={"home_form": True})
    result = select_recommendations([c], SelectionConfig(), now=NOW)
    check("candidate missing required market data never reaches MAIN", len(result.main) == 0)


def test_small_sample_penalises_confidence():
    strong = strong_candidate("ev1", league="League-1")
    thin = make("ev2", 2.4, 0.84, sample_size=3, league="League-2")
    result = select_recommendations([strong, thin], SelectionConfig(), now=NOW)
    strong_conf = next(c.confidence_score for c in (result.main + result.reserve) if c.event_id == "ev1")
    thin_conf = next(c.confidence_score for c in (result.main + result.reserve + result.avoid + result.insufficient_data)
                      if c.event_id == "ev2")
    check("a tiny sample size yields lower confidence than a well-sampled equivalent", thin_conf < strong_conf, (thin_conf, strong_conf))


def run():
    test_empty_input_returns_no_recommendation_result()
    test_weak_candidates_are_never_padded_into_main()
    test_strong_candidates_reach_main()
    test_max_five_main_cap()
    test_fewer_than_five_allowed()
    test_reserve_cap_of_three()
    test_one_main_per_event()
    test_negative_ev_rejected_even_with_high_probability()
    test_correct_score_is_high_risk_by_default()
    test_deterministic_output()
    test_missing_required_market_data_excludes_candidate()
    test_small_sample_penalises_confidence()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
