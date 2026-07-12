"""
Unit tests for ai_predictions/matching.py: real-shaped row extraction,
validation, deduplication, normalization and grouping -- the exact
pipeline stage that a "0 matched markets in production" bug report would
implicate. No network calls.
"""

import sys

sys.path.insert(0, ".")

from ai_predictions.matching import (
    ValidationStats,
    build_event_key,
    canonical_outcome,
    dedupe_bookmaker_rows,
    extract_rows,
    group_rows,
    normalize_point,
    normalize_price,
    normalize_text,
    validate_rows,
)

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def _event(bookmakers, home="Arsenal", away="Chelsea"):
    return {
        "id": "evt-1",
        "_sport_key": "soccer_epl",
        "home_team": home,
        "away_team": away,
        "commence_time": "2026-07-13T12:00:00Z",
        "bookmakers": bookmakers,
    }


def _h2h_bookmaker(title, home_price, draw_price, away_price, home="Arsenal", away="Chelsea", last_update="2026-07-12T10:00:00Z"):
    return {
        "title": title,
        "last_update": last_update,
        "markets": [{"key": "h2h", "outcomes": [
            {"name": home, "price": home_price},
            {"name": "Draw", "price": draw_price},
            {"name": away, "price": away_price},
        ]}],
    }


def test_event_key_excludes_bookmaker_and_is_stable():
    k1 = build_event_key("soccer_epl", "2026-07-13T12:00:00Z", "Arsenal", "Chelsea")
    k2 = build_event_key("soccer_epl", "2026-07-13T12:00:00Z", "Arsenal", "Chelsea")
    check("identical event data always produces the identical key", k1 == k2)


def test_point_normalization_2_5_equals_2_50():
    check("2.5 and 2.50 normalize to the same point", normalize_point(2.5) == normalize_point("2.50"))
    check("missing point normalizes to None, never guessed", normalize_point(None) is None)
    check("non-numeric point normalizes to None", normalize_point("n/a") is None)


def test_price_validation_rejects_non_numeric_and_le_1():
    check("price of 1.0 is invalid (never a real decimal price)", normalize_price(1.0) is None)
    check("negative price is invalid", normalize_price(-2.0) is None)
    check("non-numeric price is invalid", normalize_price("abc") is None)
    check("valid price passes through as float", normalize_price("2.05") == 2.05)


def test_unicode_and_case_and_whitespace_normalization_match_teams():
    # Bookmaker A uses combining-character accents / stray whitespace / different case;
    # bookmaker B uses precomposed accents. Both name the same real team.
    a = "Bayer   München"
    b = "bayer münchen"
    check("whitespace + case differences normalize to the same value", normalize_text(a) == normalize_text(b))
    outcome_a = canonical_outcome("h2h", "  ARSENAL ", "Arsenal", "Chelsea")
    check("case/whitespace variant of home team name still canonicalizes to HOME", outcome_a == "HOME")


def test_h2h_lay_is_unsupported_not_merged():
    event = _event([
        _h2h_bookmaker("BookA", 2.00, 3.30, 4.00),
        {"title": "ExchangeBook", "last_update": "2026-07-12T10:00:00Z", "markets": [
            {"key": "h2h_lay", "outcomes": [{"name": "Arsenal", "price": 2.10}, {"name": "Chelsea", "price": 3.90}]},
        ]},
    ])
    rows = extract_rows(event, event_id="evt-1", league="EPL")
    stats = ValidationStats()
    valid = validate_rows(rows, stats)
    check("h2h_lay rows counted separately as unsupported", stats.unsupported_markets_seen.get("h2h_lay") == 2)
    check("h2h_lay never appears in the validated row set", all(r.market != "h2h_lay" for r in valid))


def test_duplicate_bookmaker_keeps_newest():
    event = _event([
        _h2h_bookmaker("BookA", 1.90, 3.30, 4.00, last_update="2026-07-12T09:00:00Z"),
        _h2h_bookmaker("BookA", 2.10, 3.30, 4.00, last_update="2026-07-12T11:00:00Z"),
        _h2h_bookmaker("BookB", 2.00, 3.30, 4.00),
        _h2h_bookmaker("BookC", 2.00, 3.30, 4.00),
    ])
    rows = extract_rows(event, event_id="evt-1", league="EPL")
    stats = ValidationStats()
    valid = validate_rows(rows, stats)
    deduped = dedupe_bookmaker_rows(valid, stats)
    groups = group_rows(deduped)
    group = next(iter(groups.values()))
    home_prices = {price for bm, price, _point in group.outcomes["HOME"] if bm == "BookA"}
    check("only the newest BookA price for HOME survives", home_prices == {2.10}, home_prices)
    check("duplicate bookmaker rows are counted, not silently dropped", stats.duplicate_bookmaker_rows >= 1)


def test_row_validation_rejection_counters():
    event = _event([
        _h2h_bookmaker("BookA", 2.00, 3.30, 4.00),
        {"title": "", "last_update": "2026-07-12T10:00:00Z", "markets": [
            {"key": "h2h", "outcomes": [{"name": "Arsenal", "price": 2.00}]},
        ]},
        {"title": "BookD", "last_update": "2026-07-12T10:00:00Z", "markets": [
            {"key": "h2h", "outcomes": [{"name": "Arsenal", "price": "not-a-number"}]},
        ]},
        {"title": "BookE", "last_update": "2026-07-12T10:00:00Z", "markets": [
            {"key": "totals", "outcomes": [{"name": "Over", "price": 1.9}]},  # missing point
        ]},
    ])
    rows = extract_rows(event, event_id="evt-1", league="EPL")
    stats = ValidationStats()
    validate_rows(rows, stats)
    check("missing bookmaker rows are rejected and counted", stats.rejected_missing_bookmaker >= 1, stats.rejected_missing_bookmaker)
    check("invalid price rows are rejected and counted", stats.rejected_invalid_price >= 1, stats.rejected_invalid_price)
    check("missing point on totals is rejected and counted", stats.rejected_missing_point >= 1, stats.rejected_missing_point)


def test_group_by_event_market_point_not_bookmaker():
    event = _event([
        _h2h_bookmaker("BookA", 2.00, 3.30, 4.00),
        _h2h_bookmaker("BookB", 1.98, 3.35, 4.05),
        _h2h_bookmaker("BookC", 2.02, 3.25, 3.95),
    ])
    rows = extract_rows(event, event_id="evt-1", league="EPL")
    stats = ValidationStats()
    valid = validate_rows(rows, stats)
    deduped = dedupe_bookmaker_rows(valid, stats)
    groups = group_rows(deduped)
    check("all three bookmakers merge into exactly one (event, market, point) group", len(groups) == 1, groups.keys())
    group = next(iter(groups.values()))
    check("the HOME outcome sees all 3 independent bookmakers", group.bookmaker_count("HOME") == 3)


def test_3_plus_bookmaker_rule_via_bookmaker_count():
    event = _event([
        _h2h_bookmaker("BookA", 2.00, 3.30, 4.00),
        _h2h_bookmaker("BookB", 1.98, 3.35, 4.05),
    ])
    rows = extract_rows(event, event_id="evt-1", league="EPL")
    stats = ValidationStats()
    valid = validate_rows(rows, stats)
    deduped = dedupe_bookmaker_rows(valid, stats)
    groups = group_rows(deduped)
    group = next(iter(groups.values()))
    check("only 2 bookmakers -> below the 3-bookmaker matching threshold", group.bookmaker_count("HOME") < 3)


def run():
    test_event_key_excludes_bookmaker_and_is_stable()
    test_point_normalization_2_5_equals_2_50()
    test_price_validation_rejects_non_numeric_and_le_1()
    test_unicode_and_case_and_whitespace_normalization_match_teams()
    test_h2h_lay_is_unsupported_not_merged()
    test_duplicate_bookmaker_keeps_newest()
    test_row_validation_rejection_counters()
    test_group_by_event_market_point_not_bookmaker()
    test_3_plus_bookmaker_rule_via_bookmaker_count()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
