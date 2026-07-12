"""
Russian-language report rendering for the cross-bookmaker value-detection
strategy. Mirrors selection_engine/report.py's tone (plain, honest, ends
with the mandatory disclaimer) but describes real market divergence
instead of a statistics-based confidence score.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List

from ai_predictions.value_engine import MIN_BOOKMAKERS, MIN_EDGE, ValueCandidate
from ai_predictions.value_selector import ValueSelectionResult
from selection_engine.report import DISCLAIMER

MARKET_DISPLAY_NAMES = {
    "1x2": "Исход матча (1X2)",
    "double_chance": "Двойной шанс",
    "draw_no_bet": "Без ничьей",
    "total_goals": "Тотал голов",
    "spread": "Гандикап",
}


def _fmt_pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


@dataclass
class Diagnostics:
    events_received: int = 0
    events_excluded_by_window: int = 0
    events_in_window: int = 0
    rows_total: int = 0
    rows_valid: int = 0
    unique_events: int = 0
    unique_groups: int = 0
    groups_with_1_bookmaker: int = 0
    groups_with_2_bookmakers: int = 0
    groups_with_3plus_bookmakers: int = 0
    markets_matched: int = 0
    candidates_created: int = 0
    candidates_rejected: int = 0
    final_recommendations: int = 0
    duplicate_bookmaker_rows: int = 0
    unsupported_markets_seen: Dict[str, int] = field(default_factory=dict)
    top_rejection_reasons: List[str] = field(default_factory=list)

    def as_lines(self) -> List[str]:
        lines = [
            f"Событий получено: {self.events_received}",
            f"Исключено окном 36ч: {self.events_excluded_by_window}",
            f"Событий в окне 36ч: {self.events_in_window}",
            f"Строк котировок получено: {self.rows_total}",
            f"Строк прошло валидацию: {self.rows_valid}",
            f"Уникальных событий: {self.unique_events}",
            f"Уникальных групп (событие+рынок+линия): {self.unique_groups}",
            f"Групп с 1 букмекером: {self.groups_with_1_bookmaker}",
            f"Групп с 2 букмекерами: {self.groups_with_2_bookmakers}",
            f"Групп с 3+ букмекерами: {self.groups_with_3plus_bookmakers}",
            f"Рынков сопоставлено (matched, 3+ букмекера): {self.markets_matched}",
            f"Кандидатов создано: {self.candidates_created}",
            f"Кандидатов отклонено: {self.candidates_rejected}",
            f"Итоговых рекомендаций: {self.final_recommendations}",
            f"Дублей букмекеров (одна и та же ставка дважды): {self.duplicate_bookmaker_rows}",
        ]
        if self.unsupported_markets_seen:
            unsupported = ", ".join(f"{k} ({v})" for k, v in sorted(self.unsupported_markets_seen.items()))
            lines.append(f"Неподдерживаемые рынки в ответе API (не смешиваются, не отброшены молча): {unsupported}")
        return lines


def _render_candidate(candidate: ValueCandidate, index: int) -> str:
    market_name = MARKET_DISPLAY_NAMES.get(candidate.market_type, candidate.market_type)
    line_part = f" {candidate.line:+g}" if candidate.line is not None else ""
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


def compute_top_rejection_reasons(rejected: List[ValueCandidate], limit: int = 10) -> List[str]:
    """Ranks rejection reasons by how often real candidates hit them --
    frequency, not alphabetical order, so the most common real blocker
    surfaces first."""
    counter: Counter = Counter()
    for candidate in rejected:
        for reason in candidate.rejection_reasons:
            counter[reason] += 1
    return [f"{reason} (x{count})" for reason, count in counter.most_common(limit)]


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
        if diagnostics.events_in_window == 0:
            lines.append(
                f"- Нет ни одного события в ближайшие 36 часов (событий получено всего: "
                f"{diagnostics.events_received}, все исключены окном 36ч). Это не ошибка сопоставления "
                f"— рынок просто не предлагает матчей в этом окне прямо сейчас."
            )
        elif diagnostics.candidates_created == 0:
            lines.append("- Не найдено ни одного реального исхода с котировками нескольких букмекеров в событиях внутри окна 36 часов.")
        else:
            for reason in diagnostics.top_rejection_reasons[:8]:
                lines.append(f"- {reason}")
        lines.append(
            f"- Требуется минимум {MIN_BOOKMAKERS} букмекера и реальное расхождение не менее "
            f"+{MIN_EDGE:.2f} — система не подбирает слабые сигналы искусственно, чтобы заполнить квоту."
        )
        lines.append("")

    lines.append("Диагностика:")
    lines.extend(diagnostics.as_lines())
    if diagnostics.top_rejection_reasons:
        lines.append("Топ причин отклонения:")
        for reason in diagnostics.top_rejection_reasons:
            lines.append(f"- {reason}")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
