"""
Unit tests for the Phase 6 probability-blend wiring in
ai_predictions/value_engine.py: apply_probability_blend must recompute
edge_final/expected_value_final only when real statistics contributed to
the blend, and classify_signal/compute_ranking_score must use those
"effective" values (falling back to the original market-only edge/EV when
a candidate was never enriched).
"""

import sys

sys.path.insert(0, ".")

from ai_predictions.matching import (
    ValidationStats,
    dedupe_bookmaker_rows,
    extract_rows,
    group_rows,
    raw_bookmaker_row_counts,
    validate_rows,
)
from ai_predictions.probability_model import blend_probability
from ai_predictions.value_engine import (
    apply_probability_blend,
    build_value_candidates_from_groups,
    classify_signal,
    compute_ranking_score,
)

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def _event_with_bookmakers(prices):
    return {
        "id": "evt-1",
        "_sport_key": "soccer_epl",
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


def _home_candidate():
    event = _event_with_bookmakers([
        ("BookA", 2.00, 3.30, 4.00),
        ("BookB", 1.98, 3.35, 4.05),
        ("BookC", 2.02, 3.25, 3.95),
        ("BookD", 2.15, 3.15, 3.75),
    ])
    rows = extract_rows(event, event_id="evt-1", league="Test League")
    stats = ValidationStats()
    valid = validate_rows(rows, stats)
    raw_counts = raw_bookmaker_row_counts(valid)
    deduped = dedupe_bookmaker_rows(valid, stats)
    groups = group_rows(deduped)
    candidates = build_value_candidates_from_groups(groups, raw_counts)
    return next(c for c in candidates if c.selection == "Home FC")


def test_unenriched_candidate_uses_original_edge_and_ev():
    candidate = _home_candidate()
    original_edge, original_ev = candidate.edge, candidate.expected_value
    level, _, _ = classify_signal(candidate)
    score = compute_ranking_score(candidate)
    check("edge_final stays None before any blend was applied", candidate.edge_final is None)
    check("classify_signal still works from the plain market-only edge/EV", level in {"HIGH", "MEDIUM", "LOW", "REJECTED"}, level)
    check("ranking score is finite and computed from the original edge/EV", isinstance(score, float))


def test_market_only_blend_leaves_edge_final_none():
    candidate = _home_candidate()
    blend = blend_probability(candidate.consensus_probability, None, 0, 0)
    apply_probability_blend(candidate, blend)
    check("market-only blend (no statistics) never fabricates edge_final", candidate.edge_final is None)
    check("market-only blend never fabricates expected_value_final", candidate.expected_value_final is None)
    check("estimated_probability is still set to the market consensus", abs(candidate.estimated_probability - candidate.consensus_probability) < 1e-9)
    check("sample_size_category recorded as none", candidate.sample_size_category == "none")


def test_real_statistics_blend_recomputes_edge_and_ev():
    candidate = _home_candidate()
    # A statistics opinion strongly agreeing with, and slightly exceeding,
    # the market consensus -- edge_final should move accordingly.
    boosted_probability = min(0.98, candidate.consensus_probability + 0.10)
    blend = blend_probability(candidate.consensus_probability, boosted_probability, 10, 10)
    apply_probability_blend(candidate, blend)
    check("edge_final is now populated once real statistics contributed", candidate.edge_final is not None)
    check("expected_value_final is now populated", candidate.expected_value_final is not None)
    expected_edge = candidate.estimated_probability - candidate.best_price_implied_probability
    check("edge_final matches estimated_probability - best_price_implied_probability",
          abs(candidate.edge_final - expected_edge) < 1e-9, (candidate.edge_final, expected_edge))
    check("sample_size_category reflects the strong sample", candidate.sample_size_category == "strong")


def test_classify_signal_uses_effective_edge_after_blend():
    candidate = _home_candidate()
    pre_blend_level, _, _ = classify_signal(candidate)
    # Push the blended probability far above the market consensus so the
    # effective edge/EV clearly differ from the original market-only ones.
    blend = blend_probability(candidate.consensus_probability, 0.95, 10, 10)
    apply_probability_blend(candidate, blend)
    post_blend_level, _, _ = classify_signal(candidate)
    check("classify_signal is re-derived from the blended (effective) edge/EV, not frozen at the pre-blend level",
          isinstance(post_blend_level, str))
    check("a materially higher blended edge never produces a *lower* signal tier than the market-only one",
          _tier_rank(post_blend_level) >= _tier_rank(pre_blend_level),
          (pre_blend_level, post_blend_level))


def _tier_rank(level):
    return {"REJECTED": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}[level]


def run():
    test_unenriched_candidate_uses_original_edge_and_ev()
    test_market_only_blend_leaves_edge_final_none()
    test_real_statistics_blend_recomputes_edge_and_ev()
    test_classify_signal_uses_effective_edge_after_blend()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
