"""
Russian-language report rendering for the cross-bookmaker value-detection
strategy. Mirrors selection_engine/report.py's tone (plain, honest, ends
with the mandatory disclaimer) but describes real market divergence
instead of a statistics-based confidence score.
"""

from __future__ import annotations

from typing import List

from ai_predictions.value_engine import MIN_BOOKMAKERS, MIN_EDGE, ValueCandidate
from ai_predictions.value_selector import ValueSelectionResult
from selection_engine.report import DISCLAIMER

MARKET_DISPLAY_NAMES = {
    "1x2": "Исход матча (1X2)",
    "double_chance": "Двойной шанс",
    "draw_no_bet": "Без ничьей",
    "total_goals": "Тотал голов",
}


def _fmt_pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


class Diagnostics:
    def __init__(self) -> None:
        self.events_received = 0
        self.markets_compared = 0
        self.candidates_created = 0
        self.candidates_rejected = 0
        self.final_recommendations = 0

    def as_lines(self) -> List[str]:
        return [
            f"Событий получено: {self.events_received}",
            f"Рынков сопоставлено: {self.markets_compared}",
            f"Кандидатов создано: {self.candidates_created}",
            f"Кандидатов отклонено: {self.candidates_rejected}",
            f"Итоговых рекомендаций: {self.final_recommendations}",
        ]


def _render_candidate(candidate: ValueCandidate, index: int) -> str:
    market_name = MARKET_DISPLAY_NAMES.get(candidate.market_type, candidate.market_type)
    line_part = f" {candidate.line}" if candidate.line is not None else ""
    return (
        f"{index}. {candidate.home_team} — {candidate.away_team}\n"
        f"   Рынок: {market_name}{line_part}\n"
        f"   Выбор: {candidate.selection}\n"
        f"   Лучшая цена: {candidate.best_price:.2f} ({candidate.best_bookmaker})\n"
        f"   Справедливая цена (по остальным {candidate.consensus_bookmaker_count} букмекерам): {candidate.fair_price:.2f}\n"
        f"   Расхождение: {_fmt_pct(candidate.edge)} | Ожидаемая ценность: {_fmt_pct(candidate.expected_value)}\n"
        f"   Букмекеров по этому исходу: {candidate.bookmaker_count}\n"
        f"   Начало: {candidate.match_datetime}"
    )


def render_value_report(result: ValueSelectionResult, diagnostics: Diagnostics) -> str:
    lines = ["AI Ставки — рыночные расхождения (реальные коэффициенты, без статистики)\n"]
    lines.append(
        "Метод: сравнение реальных коэффициентов нескольких букмекеров по одному и тому же "
        "исходу. Никакая статистика команд не используется и не изобретается — только реальные цены.\n"
    )

    if result.main:
        lines.append(f"Найдено рекомендаций: {len(result.main)}\n")
        for i, candidate in enumerate(result.main, start=1):
            lines.append(_render_candidate(candidate, i))
            lines.append("")
    else:
        lines.append("Сегодня нет надёжных рекомендаций.\n")
        lines.append("Причины:")
        if diagnostics.candidates_created == 0:
            lines.append("- Не найдено ни одного реального исхода с котировками нескольких букмекеров в ближайшие 36 часов.")
        else:
            reasons = set()
            for c in result.rejected:
                reasons.update(c.rejection_reasons)
            for reason in sorted(reasons)[:8]:
                lines.append(f"- {reason}")
        lines.append(
            f"- Требуется минимум {MIN_BOOKMAKERS} букмекера и реальное расхождение не менее "
            f"+{MIN_EDGE:.2f} — система не подбирает слабые сигналы искусственно, чтобы заполнить квоту."
        )
        lines.append("")

    lines.append("Диагностика:")
    lines.extend(diagnostics.as_lines())
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
