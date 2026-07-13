"""
Unit tests for ai_predictions/value_report.py: section order, wording
guardrails ("safe"/"guaranteed" language must never appear), per-candidate
field rendering, and honest empty-state messaging.
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


def test_section_order_is_high_medium_low_rejected_diagnostics():
    result = ValueSelectionResult(
        high=[_candidate("evt-1", "1x2", "Home FC", "HIGH")],
        medium=[_candidate("evt-2", "1x2", "Home FC", "MEDIUM")],
        low=[_candidate("evt-3", "1x2", "Home FC", "LOW")],
    )
    diag = Diagnostics(high_count=1, medium_count=1, low_count=1, rejected_count=0)
    report = render_value_report(result, diag)
    high_i = report.index("🔥 HIGH")
    medium_i = report.index("🟡 MEDIUM")
    low_i = report.index("⚪ LOW")
    rejected_i = report.index("Отклонено кандидатов")
    diag_i = report.index("Диагностика:")
    check("sections render in fixed order HIGH -> MEDIUM -> LOW -> rejected summary -> diagnostics",
          high_i < medium_i < low_i < rejected_i < diag_i, (high_i, medium_i, low_i, rejected_i, diag_i))


def test_report_never_uses_forbidden_confidence_words_for_low():
    # The disclaimer legitimately says "not a guarantee" -- that's a
    # negation, not a positive claim. What must never appear is a LOW
    # signal being positively labelled a safe/guaranteed/sure bet.
    result = ValueSelectionResult(low=[_candidate("evt-1", "1x2", "Home FC", "LOW")])
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
    result = ValueSelectionResult(high=[candidate])
    diag = Diagnostics(high_count=1)
    report = render_value_report(result, diag)
    check("best price is rendered", "2.35" in report)
    check("fair price is rendered", "2.10" in report)
    check("bookmaker count is rendered", "Букмекеров: 4" in report)
    check("signal level label is rendered", "HIGH" in report)


def test_two_bookmaker_candidate_gets_reduced_confidence_note():
    candidate = _candidate("evt-1", "1x2", "Home FC", "MEDIUM", unique_bookmaker_count=2)
    result = ValueSelectionResult(medium=[candidate])
    diag = Diagnostics(medium_count=1)
    report = render_value_report(result, diag)
    check("2-bookmaker candidate carries an explicit reduced-confidence note",
          "2 независимых букмекера" in report)


def test_outlier_warning_is_rendered_when_present():
    candidate = _candidate("evt-1", "1x2", "Home FC", "MEDIUM", outlier_warning="Цена сильно выделяется на фоне рынка.")
    result = ValueSelectionResult(medium=[candidate])
    diag = Diagnostics(medium_count=1)
    report = render_value_report(result, diag)
    check("outlier warning text is surfaced in the report", "сильно выделяется" in report)


def test_empty_result_gives_honest_no_signals_message():
    result = ValueSelectionResult()
    diag = Diagnostics(events_received=0, events_in_window=0)
    report = render_value_report(result, diag)
    check("empty result explicitly says no signals at any level, does not invent one",
          "нет сигналов" in report.lower())


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
    test_section_order_is_high_medium_low_rejected_diagnostics()
    test_report_never_uses_forbidden_confidence_words_for_low()
    test_report_includes_explicit_non_advice_disclaimer()
    test_candidate_fields_all_rendered()
    test_two_bookmaker_candidate_gets_reduced_confidence_note()
    test_outlier_warning_is_rendered_when_present()
    test_empty_result_gives_honest_no_signals_message()
    test_compute_top_rejection_reasons_orders_by_frequency()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
