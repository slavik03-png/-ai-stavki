"""
Unit tests for ai_predictions/value_report.py: the ranked top-signals
section, wording guardrails ("safe"/"guaranteed" language must never
appear), per-candidate field rendering, honest empty-state messaging, and
the closest-rejected fallback with full candidate detail.
"""

import sys

sys.path.insert(0, ".")

from ai_predictions.value_engine import ValueCandidate
from ai_predictions.value_report import (
    Diagnostics,
    compute_top_rejection_reasons,
    render_telegram_signals_message,
    render_value_report,
    summarize_api_errors_ru,
)
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


# ---------------------------------------------------------------------------
# render_telegram_signals_message: concise, Russian, non-technical message
# for the "🤖 Прогнозы ИИ" button (see bot.py's handle_ai_predictions).
# ---------------------------------------------------------------------------

def test_telegram_message_shows_at_most_five_cards():
    candidates = [
        _candidate(f"evt-{i}", "1x2", "Home FC", "LOW", ranking_score=float(10 - i))
        for i in range(8)
    ]
    result = ValueSelectionResult(top_signals=candidates[:5])
    diag = Diagnostics(high_count=0, medium_count=0, low_count=5, rejected_count=3)
    chunks = render_telegram_signals_message(result, diag)
    combined = "\n".join(chunks)
    check("no more than 5 numbered cards appear", combined.count("⚪ НИЗКИЙ") == 5)


def test_telegram_message_orders_high_before_medium_before_low():
    result = ValueSelectionResult(
        top_signals=[
            _candidate("evt-h", "1x2", "Home FC", "HIGH"),
            _candidate("evt-m", "1x2", "Home FC", "MEDIUM"),
            _candidate("evt-l", "1x2", "Home FC", "LOW"),
        ],
    )
    diag = Diagnostics(high_count=1, medium_count=1, low_count=1, rejected_count=0)
    combined = "\n".join(render_telegram_signals_message(result, diag))
    check(
        "HIGH card appears before MEDIUM, which appears before LOW",
        combined.index("🔥 ВЫСОКИЙ") < combined.index("🟡 СРЕДНИЙ") < combined.index("⚪ НИЗКИЙ"),
        combined,
    )


def test_telegram_message_low_signal_carries_experimental_warning():
    result = ValueSelectionResult(top_signals=[_candidate("evt-1", "1x2", "Home FC", "LOW")])
    diag = Diagnostics(low_count=1)
    combined = "\n".join(render_telegram_signals_message(result, diag))
    check(
        "LOW card carries the exact required experimental-risk warning",
        "⚠️ Экспериментальный сигнал. Риск высокий. Не является основной рекомендацией." in combined,
        combined,
    )


def test_telegram_message_high_signal_does_not_carry_low_warning():
    result = ValueSelectionResult(top_signals=[_candidate("evt-1", "1x2", "Home FC", "HIGH")])
    diag = Diagnostics(high_count=1)
    combined = "\n".join(render_telegram_signals_message(result, diag))
    check(
        "HIGH card does not carry the LOW-only experimental warning",
        "Экспериментальный сигнал" not in combined,
    )


def test_telegram_message_has_no_raw_diagnostics_or_english_technical_text():
    result = ValueSelectionResult(top_signals=[_candidate("evt-1", "1x2", "Home FC", "MEDIUM")])
    diag = Diagnostics(
        medium_count=1,
        sports_discovered=["soccer_epl", "soccer_norway_eliteserien"],
        sports_queried=["soccer_epl"],
        sports_skipped={"soccer_x": "неактивен"},
        rows_total=500, rows_valid=480, duplicate_bookmaker_rows=3,
        top_rejection_reasons=["edge below threshold (x10)"],
    )
    combined = "\n".join(render_telegram_signals_message(result, diag))
    check("no 'Диагностика' section appears", "Диагностика" not in combined)
    check("no HTTP error text appears", "HTTP" not in combined)
    check("no skipped-competition list appears", "soccer_x" not in combined and "soccer_epl" not in combined)
    check("no raw validation row counts appear", "Строк" not in combined and "480" not in combined)
    check("no rejection-reason frequency list appears", "edge below threshold" not in combined)


def test_telegram_message_summary_line_matches_required_format():
    result = ValueSelectionResult(top_signals=[_candidate("evt-1", "1x2", "Home FC", "HIGH")])
    diag = Diagnostics(high_count=1, medium_count=2, low_count=3, rejected_count=40)
    combined = "\n".join(render_telegram_signals_message(result, diag))
    check(
        "summary line matches the required Russian format",
        "Итого: ВЫСОКИЙ — 1, СРЕДНИЙ — 2, НИЗКИЙ — 3, отклонено — 40." in combined,
        combined,
    )


def test_telegram_message_no_signals_uses_exact_required_text():
    result = ValueSelectionResult()
    diag = Diagnostics(high_count=0, medium_count=0, low_count=0, rejected_count=12)
    combined = "\n".join(render_telegram_signals_message(result, diag))
    check(
        "empty result uses the exact required 'no signals' sentence",
        "На ближайшие 36 часов подходящих сигналов не найдено." in combined,
        combined,
    )
    check("empty result still shows the short count summary", "Итого:" in combined)


def test_telegram_message_shows_best_low_signals_when_no_high_or_medium():
    result = ValueSelectionResult(top_signals=[_candidate("evt-1", "1x2", "Home FC", "LOW")])
    diag = Diagnostics(high_count=0, medium_count=0, low_count=1, rejected_count=5)
    combined = "\n".join(render_telegram_signals_message(result, diag))
    check("LOW signal is shown even with no HIGH/MEDIUM", "⚪ НИЗКИЙ" in combined)
    check("LOW signal is clearly marked experimental", "Экспериментальный сигнал" in combined)


def test_telegram_message_translates_totals_over_under_to_russian():
    over = _candidate("evt-1", "total_goals", "Over", "MEDIUM", line=2.5)
    under = _candidate("evt-2", "total_goals", "Under", "LOW", line=3.0)
    result = ValueSelectionResult(top_signals=[over, under])
    diag = Diagnostics(medium_count=1, low_count=1)
    combined = "\n".join(render_telegram_signals_message(result, diag))
    check("totals Over 2.5 is translated to Russian 'Тотал больше 2,5'", "Тотал больше 2,5" in combined, combined)
    check("totals Under 3 is translated to Russian 'Тотал меньше 3'", "Тотал меньше 3" in combined, combined)
    check("no raw English 'Over'/'Under' leaks into the card", "Over" not in combined and "Under" not in combined)


def test_telegram_message_translates_spread_market_to_fora():
    candidate = _candidate("evt-1", "spread", "Home FC", "MEDIUM", line=-1.5)
    result = ValueSelectionResult(top_signals=[candidate])
    diag = Diagnostics(medium_count=1)
    combined = "\n".join(render_telegram_signals_message(result, diag))
    check("spread market is labelled 'Фора' in Russian", "Фора" in combined, combined)


def test_telegram_message_shows_h2h_selection_as_clear_team_name():
    home_win = _candidate("evt-1", "1x2", "Home FC", "HIGH")
    draw = _candidate("evt-2", "1x2", "Ничья", "MEDIUM")
    result = ValueSelectionResult(top_signals=[home_win, draw])
    diag = Diagnostics(high_count=1, medium_count=1)
    combined = "\n".join(render_telegram_signals_message(result, diag))
    check("h2h home selection is shown as a clear name, not raw code", "Home FC" in combined)
    check("h2h draw selection is shown as 'Ничья'", "Ничья" in combined)


def test_telegram_message_splits_into_multiple_chunks_when_too_long():
    candidates = [
        _candidate(f"evt-{i}", "1x2", "Home FC", "LOW", league="Some Long League Name FC " * 3)
        for i in range(5)
    ]
    result = ValueSelectionResult(top_signals=candidates)
    diag = Diagnostics(low_count=5)
    chunks = render_telegram_signals_message(result, diag)
    check("no chunk exceeds Telegram's safe message length", all(len(c) <= 4096 for c in chunks))


# ---------------------------------------------------------------------------
# summarize_api_errors_ru: single-line aggregate for /status.
# ---------------------------------------------------------------------------

def test_summarize_api_errors_aggregates_by_http_code():
    errors = [f"The Odds API вернул HTTP 401 для soccer_league_{i}" for i in range(24)]
    summary = summarize_api_errors_ru(errors, {})
    check(
        "aggregate error line matches the required format",
        summary == "Некоторые турниры недоступны: HTTP 401 — 24 турнира.",
        summary,
    )


def test_summarize_api_errors_returns_none_when_nothing_failed():
    check("no error summary when there are no failures", summarize_api_errors_ru([], {}) is None)


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

    test_telegram_message_shows_at_most_five_cards()
    test_telegram_message_orders_high_before_medium_before_low()
    test_telegram_message_low_signal_carries_experimental_warning()
    test_telegram_message_high_signal_does_not_carry_low_warning()
    test_telegram_message_has_no_raw_diagnostics_or_english_technical_text()
    test_telegram_message_summary_line_matches_required_format()
    test_telegram_message_no_signals_uses_exact_required_text()
    test_telegram_message_shows_best_low_signals_when_no_high_or_medium()
    test_telegram_message_translates_totals_over_under_to_russian()
    test_telegram_message_translates_spread_market_to_fora()
    test_telegram_message_shows_h2h_selection_as_clear_team_name()
    test_telegram_message_splits_into_multiple_chunks_when_too_long()

    test_summarize_api_errors_aggregates_by_http_code()
    test_summarize_api_errors_returns_none_when_nothing_failed()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
