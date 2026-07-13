"""
Russian-language report rendering for the ranked HIGH/MEDIUM/LOW/REJECTED
value-detection strategy. Mirrors selection_engine/report.py's tone
(plain, honest, ends with the mandatory disclaimer) but describes real
market divergence instead of a statistics-based confidence score.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List

from ai_predictions.value_config import SIGNAL_LABELS
from ai_predictions.value_engine import ValueCandidate
from ai_predictions.value_selector import ValueSelectionResult
from ai_predictions.window import parse_commence_time, format_display_time
from selection_engine.report import DISCLAIMER

MARKET_DISPLAY_NAMES = {
    "1x2": "Исход матча (1X2)",
    "double_chance": "Двойной шанс",
    "draw_no_bet": "Без ничьей",
    "total_goals": "Тотал голов",
    "spread": "Гандикап",
}

_NEVER_WORDS_NOTE = (
    "Ни один сигнал не является «безопасной», «гарантированной» или «уверенной» ставкой — "
    "это ранжирование рыночных расхождений для исследования, не финансовый совет."
)


def _fmt_pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def _fmt_local_time(iso_time: str) -> str:
    dt = parse_commence_time(iso_time)
    if dt is None:
        return iso_time
    return format_display_time(dt)


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

    # -- ranked-signal counts (Step 11) --
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    rejected_count: int = 0
    outlier_warning_count: int = 0
    remaining_odds_api_credits: "int | str | None" = None

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
            f"Рынков сопоставлено (matched, 2+ букмекера): {self.markets_matched}",
            f"Кандидатов создано: {self.candidates_created}",
            f"🔥 HIGH: {self.high_count}",
            f"🟡 MEDIUM: {self.medium_count}",
            f"⚪ LOW: {self.low_count}",
            f"Отклонено: {self.rejected_count}",
            f"Предупреждений о выбросе цены: {self.outlier_warning_count}",
            f"Дублей букмекеров (одна и та же ставка дважды): {self.duplicate_bookmaker_rows}",
        ]
        if self.remaining_odds_api_credits is not None:
            lines.append(f"Осталось кредитов The Odds API: {self.remaining_odds_api_credits}")
        if self.unsupported_markets_seen:
            unsupported = ", ".join(f"{k} ({v})" for k, v in sorted(self.unsupported_markets_seen.items()))
            lines.append(f"Неподдерживаемые рынки в ответе API (не смешиваются, не отброшены молча): {unsupported}")
        return lines


def _level_reason(candidate: ValueCandidate) -> str:
    if candidate.signal_level == "HIGH":
        return "сильная положительная ожидаемая ценность, широкое покрытие букмекеров, валидный консенсус."
    if candidate.signal_level == "MEDIUM":
        return "заметное расхождение и положительная ожидаемая ценность, но требует дополнительной осторожности."
    return "слабый/экспериментальный сигнал — только для исследования и отслеживания."


def _render_candidate(candidate: ValueCandidate, index: int) -> str:
    market_name = MARKET_DISPLAY_NAMES.get(candidate.market_type, candidate.market_type)
    line_part = f" {candidate.line:+g}" if candidate.line is not None else ""
    lines = [
        f"{index}. {SIGNAL_LABELS.get(candidate.signal_level, candidate.signal_level)}",
        "",
        f"{candidate.home_team} — {candidate.away_team}"
        + (f" ({candidate.league})" if candidate.league else ""),
        f"Дата: {_fmt_local_time(candidate.match_datetime)}",
        f"Рынок: {market_name}{line_part}",
        f"Исход: {candidate.selection}",
        f"Лучшая цена: {candidate.best_price:.2f} — {candidate.best_bookmaker}",
        f"Справедливая цена: {candidate.fair_price:.2f}",
        f"Расхождение (edge): {_fmt_pct(candidate.edge)}",
        f"Ожидаемая ценность (EV): {_fmt_pct(candidate.expected_value)}",
        f"Букмекеров: {candidate.unique_bookmaker_count}",
        f"Уровень уверенности: {candidate.signal_level}",
    ]
    if candidate.unique_bookmaker_count == 2:
        lines.append("⚠️ Только 2 независимых букмекера — сниженная уверенность.")
    if candidate.outlier_warning:
        lines.append(f"⚠️ {candidate.outlier_warning}")
    lines.append(f"Причина: {_level_reason(candidate)}")
    return "\n".join(lines)


def compute_top_rejection_reasons(rejected: List[ValueCandidate], limit: int = 10) -> List[str]:
    """Ranks rejection reasons by how often real candidates hit them --
    frequency, not alphabetical order, so the most common real blocker
    surfaces first."""
    counter: Counter = Counter()
    for candidate in rejected:
        for reason in candidate.rejection_reasons:
            counter[reason] += 1
    return [f"{reason} (x{count})" for reason, count in counter.most_common(limit)]


def _render_section(title: str, candidates: List[ValueCandidate]) -> List[str]:
    lines = [title]
    if not candidates:
        lines.append("Нет сигналов этого уровня.")
        return lines
    for i, candidate in enumerate(candidates, start=1):
        lines.append(_render_candidate(candidate, i))
        lines.append("")
    return lines


def render_value_report(result: ValueSelectionResult, diagnostics: Diagnostics) -> str:
    lines = ["AI Ставки — ранжированные рыночные сигналы (реальные коэффициенты, без статистики)\n"]
    lines.append(
        "Метод: сравнение реальных коэффициентов нескольких букмекеров по одному и тому же "
        "исходу. Никакая статистика команд не используется и не изобретается — только реальные цены. "
        "Это система ранжирования и исследования рынка, а не гарантия прибыли.\n"
    )
    lines.append(_NEVER_WORDS_NOTE)
    lines.append("")

    if not (result.high or result.medium or result.low):
        lines.append("Сегодня нет сигналов ни одного уровня (HIGH/MEDIUM/LOW).\n")
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
        lines.append("")

    lines.extend(_render_section("🔥 HIGH", result.high))
    lines.extend(_render_section("🟡 MEDIUM", result.medium))
    lines.extend(_render_section("⚪ LOW", result.low))

    lines.append(f"Отклонено кандидатов: {len(result.rejected)}")
    if diagnostics.top_rejection_reasons:
        lines.append("Топ причин отклонения:")
        for reason in diagnostics.top_rejection_reasons[:8]:
            lines.append(f"- {reason}")
    lines.append("")

    lines.append("Диагностика:")
    lines.extend(diagnostics.as_lines())
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)
