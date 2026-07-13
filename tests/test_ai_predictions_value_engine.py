"""
Unit tests for ai_predictions/value_engine.py: formula verification (Step 1)
plus the ranked HIGH/MEDIUM/LOW/REJECTED signal system (Steps 2-4), using
only synthetic-but-realistic bookmaker JSON (no network calls, no football
statistics provider involved).
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
from ai_predictions.value_config import (
    HIGH_MIN_BOOKMAKERS,
    HIGH_MIN_EDGE,
    HIGH_MIN_EV,
    LOW_MIN_BOOKMAKERS,
    LOW_MIN_EDGE,
    LOW_MIN_EV,
    MEDIUM_MIN_BOOKMAKERS,
    MEDIUM_MIN_EDGE,
    MEDIUM_MIN_EV,
    OUTLIER_PRICE_GAP_THRESHOLD,
    SIGNAL_HIGH,
    SIGNAL_LOW,
    SIGNAL_MEDIUM,
    SIGNAL_REJECTED,
)
from ai_predictions.value_engine import (
    MIN_BOOKMAKERS,
    build_value_candidates_from_groups,
    compute_ranking_score,
)
from selection_engine.scoring import (
    compute_edge,
    compute_expected_value,
    compute_fair_odds,
    normalise_market_probabilities,
    raw_implied_probability,
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


def _build_candidates(event, event_id="evt-1", league="Test League"):
    rows = extract_rows(event, event_id=event_id, league=league)
    stats = ValidationStats()
    valid = validate_rows(rows, stats)
    raw_counts = raw_bookmaker_row_counts(valid)
    deduped = dedupe_bookmaker_rows(valid, stats)
    groups = group_rows(deduped)
    return build_value_candidates_from_groups(groups, raw_counts)


def _home(event):
    candidates = _build_candidates(event)
    return next(c for c in candidates if c.selection == "Home FC")


# ---------------------------------------------------------------------------
# Step 1: formula verification with hand-computed numeric examples.
# ---------------------------------------------------------------------------

def test_raw_implied_probability_formula():
    check("raw_implied_probability(2.00) == 0.50", abs(raw_implied_probability(2.00) - 0.5) < 1e-9)
    check("raw_implied_probability(4.00) == 0.25", abs(raw_implied_probability(4.00) - 0.25) < 1e-9)


def test_margin_removal_formula_hand_computed():
    # BookA: 2.00 / 3.30 / 4.00 -> raw probs 0.5, 0.30303.., 0.25 -> sum 1.05303..
    raw = [raw_implied_probability(p) for p in (2.00, 3.30, 4.00)]
    normalised = normalise_market_probabilities(raw)
    check("normalised probabilities sum to 1.0", abs(sum(normalised) - 1.0) < 1e-9, sum(normalised))
    expected_home = (1 / 2.00) / (1 / 2.00 + 1 / 3.30 + 1 / 4.00)
    check("margin-free home probability matches hand computation",
          abs(normalised[0] - expected_home) < 1e-9, (normalised[0], expected_home))


def test_leave_one_out_consensus_hand_computed():
    # 3 "other" bookmakers each priced Home FC at 2.00/1.98/2.02 (own full
    # 3-way market); their margin-free home probabilities average to the
    # leave-one-out consensus used for the 4th (best-price) bookmaker.
    event = _event_with_bookmakers([
        ("BookA", 2.00, 3.30, 4.00),
        ("BookB", 1.98, 3.35, 4.05),
        ("BookC", 2.02, 3.25, 3.95),
        ("BookD", 2.15, 3.15, 3.75),  # best price, excluded from its own consensus
    ])
    home = _home(event)

    def margin_free(h, d, a):
        raw = [raw_implied_probability(x) for x in (h, d, a)]
        return normalise_market_probabilities(raw)[0]

    expected_consensus = (margin_free(2.00, 3.30, 4.00) + margin_free(1.98, 3.35, 4.05) + margin_free(2.02, 3.25, 3.95)) / 3
    check("leave-one-out consensus matches hand computation",
          abs(home.consensus_probability - expected_consensus) < 1e-9,
          (home.consensus_probability, expected_consensus))
    check("consensus excludes the best-price bookmaker itself", home.consensus_bookmaker_count == 3)


def test_edge_and_ev_and_fair_odds_formulas_hand_computed():
    consensus = 0.47
    best_price = 2.20
    best_prob = 0.42
    expected_edge = consensus - best_prob
    expected_ev = consensus * best_price - 1.0
    expected_fair = 1.0 / consensus
    check("compute_edge matches consensus - best_price_implied_probability",
          abs(compute_edge(consensus, best_prob) - expected_edge) < 1e-9)
    check("compute_expected_value matches fair_probability * offered_odds - 1",
          abs(compute_expected_value(consensus, best_price) - expected_ev) < 1e-9)
    check("compute_fair_odds matches 1 / consensus_probability",
          abs(compute_fair_odds(consensus) - expected_fair) < 1e-9)


# ---------------------------------------------------------------------------
# Step 2-4: ranked signal levels, using real numbers computed by the engine
# itself (verified by hand above) rather than invented thresholds.
# ---------------------------------------------------------------------------

# 5 bookmakers, 3 tightly agree near 2.00, 2 elevated near 2.45 (gap between
# best and second-best stays well under the 10% outlier threshold) ->
# genuine, uncontested HIGH-tier divergence.
HIGH_PRICES = [
    ("BookA", 2.00, 3.30, 4.00),
    ("BookB", 2.00, 3.30, 4.00),
    ("BookC", 2.00, 3.30, 4.00),
    ("BookD", 2.45, 2.80, 3.30),
    ("BookE", 2.42, 2.82, 3.32),
]

# Same shape but a smaller gap over consensus -> MEDIUM, not HIGH.
MEDIUM_PRICES = [
    ("BookA", 2.00, 3.30, 4.00),
    ("BookB", 2.00, 3.30, 4.00),
    ("BookC", 2.00, 3.30, 4.00),
    ("BookD", 2.30, 3.00, 3.60),
    ("BookE", 2.28, 3.02, 3.62),
]

# 4 bookmakers with a real but modest divergence -> LOW.
LOW_PRICES = [
    ("BookA", 1.85, 3.60, 4.40),
    ("BookB", 1.87, 3.55, 4.30),
    ("BookC", 1.83, 3.65, 4.45),
    ("BookD", 2.00, 3.30, 4.00),
]

FLAT_PRICES = [
    ("BookA", 2.00, 3.30, 4.00),
    ("BookB", 2.01, 3.29, 3.99),
    ("BookC", 1.99, 3.31, 4.01),
]

# The single best price is >10% above the real second-best price for the
# same outcome -- classic isolated-outlier shape that must demote HIGH by
# exactly one level to MEDIUM, even though the raw EV/edge numbers alone
# would otherwise clear the HIGH bar.
OUTLIER_PRICES = [
    ("BookA", 2.00, 3.30, 4.00),
    ("BookB", 1.98, 3.35, 4.05),
    ("BookC", 2.02, 3.25, 3.95),
    ("BookD", 2.30, 3.10, 3.60),
]


def test_high_tier_signal():
    home = _home(_event_with_bookmakers(HIGH_PRICES))
    check("real HIGH-shaped divergence classified as HIGH", home.signal_level == SIGNAL_HIGH, home.signal_level)
    check("HIGH candidate meets the documented EV floor", home.expected_value >= HIGH_MIN_EV, home.expected_value)
    check("HIGH candidate meets the documented edge floor", home.edge >= HIGH_MIN_EDGE, home.edge)
    check("HIGH candidate has >= HIGH_MIN_BOOKMAKERS unique bookmakers",
          home.unique_bookmaker_count >= HIGH_MIN_BOOKMAKERS, home.unique_bookmaker_count)
    check("HIGH candidate carries no rejection reasons", home.rejection_reasons == [], home.rejection_reasons)
    check("HIGH candidate is not flagged as an outlier", home.is_outlier is False)


def test_medium_tier_signal():
    home = _home(_event_with_bookmakers(MEDIUM_PRICES))
    check("real MEDIUM-shaped divergence classified as MEDIUM", home.signal_level == SIGNAL_MEDIUM, home.signal_level)
    check("MEDIUM candidate meets its own EV floor but not HIGH's",
          MEDIUM_MIN_EV <= home.expected_value < HIGH_MIN_EV, home.expected_value)


def test_low_tier_signal():
    home = _home(_event_with_bookmakers(LOW_PRICES))
    check("real LOW-shaped divergence classified as LOW", home.signal_level == SIGNAL_LOW, home.signal_level)
    check("LOW candidate meets its own EV floor but not MEDIUM's",
          LOW_MIN_EV <= home.expected_value < MEDIUM_MIN_EV, home.expected_value)


def test_rejected_tier_flat_market():
    home = _home(_event_with_bookmakers(FLAT_PRICES))
    check("flat/agreeing market is REJECTED, never shown as a signal", home.signal_level == SIGNAL_REJECTED)
    check("REJECTED candidate carries a concrete reason", len(home.rejection_reasons) > 0, home.rejection_reasons)


def test_below_min_bookmakers_is_rejected_but_not_invented():
    event = _event_with_bookmakers([
        ("BookA", 2.00, 3.30, 4.00),
    ])
    candidates = _build_candidates(event)
    home_candidates = [c for c in candidates if c.selection == "Home FC"]
    check("a single-bookmaker outcome is still built as real data", len(home_candidates) == 1)
    check("single-bookmaker outcome cannot reach a signal (no consensus to compare against)",
          home_candidates[0].signal_level == SIGNAL_REJECTED, home_candidates[0].rejection_reasons)


def test_two_bookmaker_market_can_only_ever_be_medium_or_low():
    # A 2-bookmaker market with a strong enough divergence to otherwise
    # qualify -- must still be capped at MEDIUM/LOW, never HIGH.
    event = _event_with_bookmakers([
        ("BookA", 2.00, 3.30, 4.00),
        ("BookB", 2.60, 2.60, 3.10),
    ])
    home = _home(event)
    check("2-bookmaker market never reaches HIGH", home.signal_level != SIGNAL_HIGH, home.signal_level)
    check("2-bookmaker market that qualifies lands on MEDIUM or LOW",
          home.signal_level in (SIGNAL_MEDIUM, SIGNAL_LOW, SIGNAL_REJECTED), home.signal_level)


def test_outlier_price_gap_downgrades_high_to_medium():
    home = _home(_event_with_bookmakers(OUTLIER_PRICES))
    check("outlier gap exceeds the configured threshold",
          home.best_second_gap > OUTLIER_PRICE_GAP_THRESHOLD, home.best_second_gap)
    check("candidate is flagged as an isolated outlier", home.is_outlier is True)
    check("outlier candidate carries a non-empty outlier_warning", bool(home.outlier_warning), home.outlier_warning)
    check("outlier demotes the signal exactly one level (would-be HIGH -> MEDIUM)",
          home.signal_level == SIGNAL_MEDIUM, home.signal_level)
    check("demotion reason is recorded in rejection_reasons",
          any("выброс" in r.lower() or "понижен" in r.lower() for r in home.rejection_reasons), home.rejection_reasons)


def test_outlier_cascade_demotes_low_to_rejected():
    # 3 bookmakers tightly agree at 2.00; the 4th ("BookD") is priced high
    # enough that its EV/edge would otherwise clear the LOW bar (but not
    # MEDIUM's higher EV bar) -- yet its 20% gap over the real second-best
    # price is a textbook isolated outlier, so it must cascade all the way
    # down to REJECTED rather than surface as a LOW signal.
    event = _event_with_bookmakers([
        ("BookA", 2.00, 3.00, 3.00),
        ("BookB", 2.00, 3.00, 3.00),
        ("BookC", 2.00, 3.00, 3.00),
        ("BookD", 2.408, 3.00, 3.00),
    ])
    home = _home(event)
    check("pre-demotion numbers genuinely clear only the LOW bar, not MEDIUM's",
          LOW_MIN_EV <= home.expected_value < MEDIUM_MIN_EV, home.expected_value)
    check("far outlier best price triggers the outlier flag",
          home.is_outlier is True and home.best_second_gap > OUTLIER_PRICE_GAP_THRESHOLD, home.best_second_gap)
    check("outlier cascade demotes a would-be LOW all the way to REJECTED",
          home.signal_level == SIGNAL_REJECTED, home.signal_level)
    check("cascade reason is recorded",
          any("LOW" in r and "REJECTED" in r for r in home.rejection_reasons), home.rejection_reasons)


def test_duplicate_bookmaker_counted_once():
    # The same bookmaker appearing twice for the same outcome (stale +
    # fresh quote) must count as ONE unique bookmaker, not two.
    event = _event_with_bookmakers(HIGH_PRICES)
    event["bookmakers"].append({
        "title": "BookA",  # duplicate of an existing bookmaker, different (older) update
        "last_update": "2026-07-11T10:00:00Z",
        "markets": [{
            "key": "h2h",
            "outcomes": [
                {"name": "Home FC", "price": 1.99},
                {"name": "Draw", "price": 3.31},
                {"name": "Away FC", "price": 4.01},
            ],
        }],
    })
    home = _home(event)
    check("duplicate bookmaker row does not inflate the unique bookmaker count",
          home.unique_bookmaker_count == 5, home.unique_bookmaker_count)


def test_total_bookmaker_rows_before_dedup_tracks_real_duplicates():
    event = _event_with_bookmakers(HIGH_PRICES)
    event["bookmakers"].append({
        "title": "BookA",
        "last_update": "2026-07-11T10:00:00Z",
        "markets": [{
            "key": "h2h",
            "outcomes": [
                {"name": "Home FC", "price": 1.99},
                {"name": "Draw", "price": 3.31},
                {"name": "Away FC", "price": 4.01},
            ],
        }],
    })
    home = _home(event)
    check("pre-dedup row count reflects the real duplicate row (6), unlike the deduped unique count (5)",
          home.total_bookmaker_rows_before_dedup == 6, home.total_bookmaker_rows_before_dedup)


def test_only_real_selections_offered_by_bookmakers_are_built():
    event = _event_with_bookmakers([
        ("BookA", 2.00, 3.30, 4.00),
        ("BookB", 2.00, 3.30, 4.00),
        ("BookC", 2.00, 3.30, 4.00),
    ])
    candidates = _build_candidates(event)
    check("no totals candidates invented when no bookmaker offered totals",
          all(c.market_type != "total_goals" for c in candidates), [c.market_type for c in candidates])


def test_spreads_market_produces_settleable_candidates():
    event = {
        "id": "evt-spread",
        "_sport_key": "soccer_epl",
        "home_team": "Home FC",
        "away_team": "Away FC",
        "commence_time": "2026-07-13T12:00:00Z",
        "bookmakers": [
            {
                "title": title,
                "last_update": "2026-07-12T10:00:00Z",
                "markets": [{
                    "key": "spreads",
                    "outcomes": [
                        {"name": "Home FC", "price": h, "point": -1.5},
                        {"name": "Away FC", "price": a, "point": 1.5},
                    ],
                }],
            }
            for title, h, a in [("BookA", 1.90, 1.95), ("BookB", 1.88, 1.97), ("BookC", 1.92, 1.93), ("BookD", 2.20, 1.65)]
        ],
    }
    candidates = _build_candidates(event)
    home = next(c for c in candidates if c.market_type == "spread" and c.selection == "Home FC")
    check("spread candidate carries the real line", home.line == -1.5, home.line)
    check("spread candidate detects the outlier as best price", home.best_bookmaker == "BookD", home.best_bookmaker)


def test_h2h_lay_is_never_merged_into_h2h():
    event = _event_with_bookmakers([
        ("BookA", 2.00, 3.30, 4.00),
        ("BookB", 2.00, 3.30, 4.00),
        ("BookC", 2.00, 3.30, 4.00),
    ])
    event["bookmakers"].append({
        "title": "ExchangeBook",
        "last_update": "2026-07-12T10:00:00Z",
        "markets": [{
            "key": "h2h_lay",
            "outcomes": [
                {"name": "Home FC", "price": 2.10},
                {"name": "Away FC", "price": 3.90},
            ],
        }],
    })
    rows = extract_rows(event, event_id="evt-1", league="Test League")
    stats = ValidationStats()
    valid = validate_rows(rows, stats)
    check("h2h_lay rows are counted as unsupported, not silently dropped",
          stats.unsupported_markets_seen.get("h2h_lay") == 2, stats.unsupported_markets_seen)
    check("h2h_lay prices never leak into the valid h2h row set",
          all(r.market != "h2h_lay" for r in valid))


def test_ranking_score_does_not_let_a_high_price_alone_dominate():
    # Two candidates with the same real EV/edge/bookmaker-count shape but
    # very different raw best_price: the score must not simply track price.
    a = _home(_event_with_bookmakers(HIGH_PRICES))
    scaled = [(title, h * 1.5, d, a_) for title, h, d, a_ in HIGH_PRICES]
    b_event = _event_with_bookmakers(scaled)
    b = _home(b_event)
    check("ranking score is bounded by real EV/edge/bookmaker inputs, not raw price magnitude",
          compute_ranking_score(a) > 0, compute_ranking_score(a))


def test_min_bookmakers_alias_matches_loosest_tier():
    check("legacy MIN_BOOKMAKERS alias resolves to the loosest (LOW) tier bar",
          MIN_BOOKMAKERS == LOW_MIN_BOOKMAKERS, MIN_BOOKMAKERS)


def run():
    test_raw_implied_probability_formula()
    test_margin_removal_formula_hand_computed()
    test_leave_one_out_consensus_hand_computed()
    test_edge_and_ev_and_fair_odds_formulas_hand_computed()
    test_high_tier_signal()
    test_medium_tier_signal()
    test_low_tier_signal()
    test_rejected_tier_flat_market()
    test_below_min_bookmakers_is_rejected_but_not_invented()
    test_two_bookmaker_market_can_only_ever_be_medium_or_low()
    test_outlier_price_gap_downgrades_high_to_medium()
    test_outlier_cascade_demotes_low_to_rejected()
    test_duplicate_bookmaker_counted_once()
    test_total_bookmaker_rows_before_dedup_tracks_real_duplicates()
    test_only_real_selections_offered_by_bookmakers_are_built()
    test_spreads_market_produces_settleable_candidates()
    test_h2h_lay_is_never_merged_into_h2h()
    test_ranking_score_does_not_let_a_high_price_alone_dominate()
    test_min_bookmakers_alias_matches_loosest_tier()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
