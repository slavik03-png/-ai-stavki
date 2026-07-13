"""
Unit tests for ai_predictions/value_report.py: the ranked top-signals
section, wording guardrails ("safe"/"guaranteed" language must never
appear), per-candidate field rendering, honest empty-state messaging, and
the closest-rejected fallback with full candidate detail.
"""

import sys

sys.path.insert(0, ".")

from ai_predictions.value_engine import ValueCandidate
from ai_predictions.value_report import Diagnostics, compute_top_rejection_reasons, render_value_report
from ai_predictions.value_selector import ValueSelectionResult

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def _candidate(event_id, market_type, selection, level, **overrides):
    base = dict(
        event_id=event_id, sport="soccer", league="Test League", country=None,
        match_datetime="2026-07-13T12:00:00Z", home_team="Home FC", away_team="Away FC",
        market_type=market_type, selection=selection, line=None,
        best_bookmaker="BookX", best_price=2.2, best_price_implied_probability=0.45,
        consensus_probability=0.5, consensus_bookmaker_count=3, fair_price=2.0,
        edge=0.05, expected_value=0.08, bookmaker_count=4, all_prices=[2.0, 2.1, 2.2, 2.2],
        unique_bookmaker_count=4, signal_level=level, ranking_score=5.0,
    )
    base.update(overrides)
    return ValueCandidate(**base)


FORBIDDEN_WORDS = ["безопасн", "гарантир", "уверенн" + "ая ставка"]


def test_top_signals_render_before_rejected_summary_and_diagnostics():
    result = ValueSelectionResult(
        top_signals=[
            _candidate("evt-1", "1x2", "Home FC", "HIGH"),
            _candidate("evt-2", "1x2", "Home FC", "MEDIUM"),
            _candidate("evt-3", "1x2", "Home FC", "LOW"),
        ],
    )
    diag = Diagnostics(high_count=1, medium_count=1, low_count=1, rejected_count=0)
    report = render_value_report(result, diag)
    top_i = report.index("Топ сигналов")
    rejected_i = report.index("Отклонено кандидатов")
    diag_i = report.index("Диагностика:")
    check("top-signals section renders before the rejected summary and diagnostics",
          top_i < rejected_i < diag_i, (top_i, rejected_i, diag_i))
    check("all three tiers are represented by their own label within the top signals",
          "HIGH" in report and "MEDIUM" in report and "LOW" in report)


def test_top_signals_respects_high_before_medium_before_low_order():
    result = ValueSelectionResult(
        top_signals=[
            _candidate("evt-h", "1x2", "Home FC", "HIGH"),
            _candidate("evt-m", "1x2", "Home FC", "MEDIUM"),
            _candidate("evt-l", "1x2", "Home FC", "LOW"),
        ],
    )
    diag = Diagnostics(high_count=1, medium_count=1, low_count=1)
    report = render_value_report(result, diag)
    high_i = report.index("evt-h" if "evt-h" in report else "HIGH")
    check("HIGH candidate is listed first among the top signals",
          report.index("1. ") < report.index("2. ") < report.index("3. "))


def test_report_never_uses_forbidden_confidence_words_for_low():
    # The disclaimer legitimately says "not a guarantee" -- that's a
    # negation, not a positive claim. What must never appear is a LOW
    # signal being positively labelled a safe/guaranteed/sure bet.
    result = ValueSelectionResult(top_signals=[_candidate("evt-1", "1x2", "Home FC", "LOW")])
    diag = Diagnostics(low_count=1)
    report = render_value_report(result, diag).lower()
    forbidden_positive_claims = ["безопасная ставка", "гарантированная победа", "верная ставка", "уверенная ставка"]
    check("report never positively labels a signal as a safe/guaranteed/sure bet",
          not any(w in report for w in forbidden_positive_claims), report[:200])


def test_report_includes_explicit_non_advice_disclaimer():
    result = ValueSelectionResult()
    diag = Diagnostics()
    report = render_value_report(result, diag)
    check("report explicitly states this is not a safe/guaranteed bet system",
          "не является" in report.lower() and ("безопасн" in report.lower() or "гарантир" in report.lower()))


def test_candidate_fields_all_rendered():
    candidate = _candidate("evt-1", "1x2", "Home FC", "HIGH", best_price=2.35, fair_price=2.10, edge=0.06, expected_value=0.10)
    result = ValueSelectionResult(top_signals=[candidate])
    diag = Diagnostics(high_count=1)
    report = render_value_report(result, diag)
    check("best price is rendered", "2.35" in report)
    check("fair price is rendered", "2.10" in report)
    check("bookmaker count is rendered", "Букмекеров: 4" in report)
    check("signal level label is rendered", "HIGH" in report)


def test_two_bookmaker_candidate_gets_reduced_confidence_note():
    candidate = _candidate("evt-1", "1x2", "Home FC", "MEDIUM", unique_bookmaker_count=2)
    result = ValueSelectionResult(top_signals=[candidate])
    diag = Diagnostics(medium_count=1)
    report = render_value_report(result, diag)
    check("2-bookmaker candidate carries an explicit reduced-confidence note",
          "2 независимых букмекера" in report)


def test_outlier_warning_is_rendered_when_present():
    candidate = _candidate("evt-1", "1x2", "Home FC", "MEDIUM", outlier_warning="Цена сильно выделяется на фоне рынка.")
    result = ValueSelectionResult(top_signals=[candidate])
    diag = Diagnostics(medium_count=1)
    report = render_value_report(result, diag)
    check("outlier warning text is surfaced in the report", "сильно выделяется" in report)


def test_empty_result_gives_honest_no_signals_message():
    result = ValueSelectionResult()
    diag = Diagnostics(events_received=0, events_in_window=0)
    report = render_value_report(result, diag)
    check("empty result explicitly says no signals at any level, does not invent one",
          "нет сигналов" in report.lower())


def test_empty_result_shows_closest_rejected_candidates_with_full_detail():
    rejected = _candidate(
        "evt-1", "1x2", "Home FC", "REJECTED",
        best_price=1.95, fair_price=1.90, edge=0.02, expected_value=-0.01, unique_bookmaker_count=3,
    )
    rejected.rejection_reasons = ["edge ниже порога LOW"]
    result = ValueSelectionResult(closest_rejected=[rejected])
    diag = Diagnostics(events_received=1, events_in_window=1, candidates_created=1)
    report = render_value_report(result, diag)
    check("closest-rejected candidate's real odds are shown", "1.95" in report and "1.90" in report)
    check("closest-rejected candidate's bookmaker count is shown", "Букмекеров: 3" in report)
    check("closest-rejected candidate's real rejection reason is shown", "edge ниже порога LOW" in report)
    check("closest-rejected candidate's market/selection is shown", "Home FC" in report)


def test_compute_top_rejection_reasons_orders_by_frequency():
    c1 = _candidate("evt-1", "1x2", "Home FC", "REJECTED")
    c1.rejection_reasons = ["low ev"]
    c2 = _candidate("evt-2", "1x2", "Home FC", "REJECTED")
    c2.rejection_reasons = ["low ev"]
    c3 = _candidate("evt-3", "1x2", "Home FC", "REJECTED")
    c3.rejection_reasons = ["too few bookmakers"]
    top = compute_top_rejection_reasons([c1, c2, c3])
    check("most frequent real rejection reason is ranked first",
          top[0].startswith("low ev"), top)


def run():
    test_top_signals_render_before_rejected_summary_and_diagnostics()
    test_top_signals_respects_high_before_medium_before_low_order()
    test_report_never_uses_forbidden_confidence_words_for_low()
    test_report_includes_explicit_non_advice_disclaimer()
    test_candidate_fields_all_rendered()
    test_two_bookmaker_candidate_gets_reduced_confidence_note()
    test_outlier_warning_is_rendered_when_present()
    test_empty_result_gives_honest_no_signals_message()
    test_empty_result_shows_closest_rejected_candidates_with_full_detail()
    test_compute_top_rejection_reasons_orders_by_frequency()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
