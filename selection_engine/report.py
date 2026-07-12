"""
Russian-language daily report rendering (spec sections 19/20).

Two distinct formats:
- `render_daily_report`: normal report with MAIN / RESERVE / HIGH_RISK /
  AVOID-explanation / model-stats sections, always ending with the
  mandatory disclaimer.
- `render_no_recommendation_report`: used whenever `result.main` is empty;
  states plainly that there are no reliable recommendations today and
  lists why, instead of forcing weak picks into the report.

No emojis are added here beyond what the surrounding bot already uses
elsewhere in the codebase; formatting stays plain so it works in any
Telegram message mode.
"""

from __future__ import annotations

from typing import List

from selection_engine.models import CandidatePrediction
from selection_engine.selector import SelectionResult
from selection_engine.versioning import AI_STAVKI_MODEL_VERSION

DISCLAIMER = (
    "Данный материал носит исключительно информационно-аналитический характер и не является "
    "гарантией результата, финансовой консультацией или призывом к ставкам. Прошлые показатели "
    "не гарантируют будущих результатов. Все решения принимаются пользователем самостоятельно и "
    "на свой риск."
)


def _fmt_pct(value) -> str:
    if value is None:
        return "н/д"
    return f"{value * 100:.0f}%" if abs(value) <= 1.0 else f"{value:.0f}%"


def _fmt_odds(value: float) -> str:
    return f"{value:.2f}"


def _render_candidate_block(candidate: CandidatePrediction, index: int) -> str:
    lines: List[str] = []
    lines.append(f"{index}. {candidate.home_team} — {candidate.away_team} ({candidate.league or 'лига не указана'})")
    lines.append(f"   Рынок: {candidate.market_type} | Исход: {candidate.selection}")
    lines.append(f"   Коэффициент: {_fmt_odds(candidate.odds)} | Букмекер: {candidate.bookmaker}")
    lines.append(
        f"   Уверенность модели: {candidate.confidence_score:.0f}/100 "
        f"| Вероятность: {_fmt_pct(candidate.model_probability)} "
        f"| Ожидаемая ценность: {candidate.expected_value:+.1%}"
        if candidate.confidence_score is not None and candidate.expected_value is not None
        else "   Уверенность модели: н/д"
    )
    lines.append(f"   Полнота данных: {_fmt_pct(candidate.data_completeness)} | Размер выборки: {candidate.sample_size} матчей")
    if candidate.historical_market_win_rate is not None:
        lines.append(
            f"   Историческая результативность рынка: {_fmt_pct(candidate.historical_market_win_rate)}"
        )
    if candidate.explanation:
        lines.append("   Обоснование: " + "; ".join(candidate.explanation))
    if candidate.risk_factors:
        lines.append("   Риски: " + "; ".join(candidate.risk_factors))
    if candidate.calibration_is_preliminary:
        lines.append("   Примечание: калибровка предварительная (недостаточно исторических данных)")
    return "\n".join(lines)


def render_no_recommendation_report(result: SelectionResult) -> str:
    lines: List[str] = []
    lines.append("AI Stavki — отчёт за сегодня")
    lines.append("")
    lines.append("Сегодня нет надёжных рекомендаций.")
    lines.append("")
    lines.append(f"Проанализировано кандидатов: {result.total_candidates_considered}")
    lines.append("Причины:")
    for reason in result.no_recommendation_reasons or ["Не найдено подходящих исходов."]:
        lines.append(f"- {reason}")
    lines.append("")
    lines.append(f"Версия модели: {AI_STAVKI_MODEL_VERSION}")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def render_daily_report(result: SelectionResult) -> str:
    if not result.has_main_recommendations:
        return render_no_recommendation_report(result)

    lines: List[str] = []
    lines.append("AI Stavki — отчёт за сегодня")
    lines.append("")
    lines.append(f"Проанализировано кандидатов: {result.total_candidates_considered}")
    lines.append(
        f"Основных рекомендаций: {len(result.main)} | Резервных: {len(result.reserve)} | "
        f"Высокий риск: {len(result.high_risk)}"
    )
    lines.append("")

    lines.append("ОСНОВНЫЕ РЕКОМЕНДАЦИИ")
    lines.append("-" * 30)
    for i, candidate in enumerate(result.main, start=1):
        lines.append(_render_candidate_block(candidate, i))
        lines.append("")

    if result.reserve:
        lines.append("РЕЗЕРВНЫЕ ВАРИАНТЫ")
        lines.append("-" * 30)
        for i, candidate in enumerate(result.reserve, start=1):
            lines.append(_render_candidate_block(candidate, i))
            lines.append("")

    if result.high_risk:
        lines.append("ВЫСОКИЙ РИСК (не для основной стратегии)")
        lines.append("-" * 30)
        for i, candidate in enumerate(result.high_risk, start=1):
            lines.append(_render_candidate_block(candidate, i))
            lines.append("")

    if result.avoid:
        lines.append("ИСКЛЮЧЕНО ИЗ РЕКОМЕНДАЦИЙ")
        lines.append("-" * 30)
        for candidate in result.avoid:
            reason = "; ".join(candidate.rejection_reasons) if candidate.rejection_reasons else "низкая уверенность"
            lines.append(
                f"- {candidate.home_team} — {candidate.away_team}, {candidate.market_type}/{candidate.selection}: {reason}"
            )
        lines.append("")

    if result.insufficient_data:
        lines.append(f"Недостаточно данных для оценки: {len(result.insufficient_data)} исход(ов)")
        lines.append("")

    lines.append(f"Версия модели: {AI_STAVKI_MODEL_VERSION}")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
