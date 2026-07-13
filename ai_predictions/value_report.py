"""
Russian-language report rendering for the ranked HIGH/MEDIUM/LOW/REJECTED
value-detection strategy. Mirrors selection_engine/report.py's tone
(plain, honest, ends with the mandatory disclaimer) but describes real
market divergence instead of a statistics-based confidence score.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

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
    "spread": "Фора",
}

#: Concise Telegram card labels for the same market types -- kept as a
#: separate dict so the full diagnostics report's existing wording never
#: changes (avoids breaking tests/test_ai_predictions_value_report.py's
#: existing expectations), while the compact user-facing card can use its
#: own short label.
TELEGRAM_MARKET_LABELS = MARKET_DISPLAY_NAMES

#: Telegram's own hard message-length limit is 4096 characters; stay well
#: under it so a long card list never gets rejected by the API.
TELEGRAM_MAX_CHARS = 3500

_LOW_RISK_WARNING = (
    "⚠️ Экспериментальный сигнал. Риск высокий. Не является основной рекомендацией."
)

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


def _fmt_line_ru(value: float) -> str:
    """Formats a numeric line the way a Russian-speaking user would write
    it by hand (comma decimal separator, no trailing .0), e.g. 2.5 -> "2,5",
    -1.0 -> "-1". Display-only -- never touches the underlying float used
    for settlement."""
    text = f"{value:g}"
    return text.replace(".", ",")


def _selection_display_ru(candidate: ValueCandidate) -> str:
    """Human, Russian-language text for 'what exactly to bet on', built
    only from real fields already on the candidate. This is a pure
    display transform: candidate.selection itself is never modified here,
    since tracking/settlement.py matches against that exact stored value.
    """
    if candidate.market_type == "total_goals":
        line_text = _fmt_line_ru(candidate.line) if candidate.line is not None else ""
        selection_lower = candidate.selection.lower()
        if selection_lower == "over":
            return f"Тотал больше {line_text}".strip()
        if selection_lower == "under":
            return f"Тотал меньше {line_text}".strip()
        return candidate.selection
    if candidate.market_type == "spread" and candidate.line is not None:
        return f"{candidate.selection} ({candidate.line:+g})"
    return candidate.selection


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

    # -- dynamic event-discovery diagnostics (production-discovery fix) --
    sports_discovered: List[str] = field(default_factory=list)
    sports_queried: List[str] = field(default_factory=list)
    sports_skipped: Dict[str, str] = field(default_factory=dict)
    discovery_source: str = "api"  # "api" or "fallback_hardcoded"
    discovery_error: "str | None" = None

    def as_lines(self) -> List[str]:
        lines = [
            f"Активных футбольных турниров обнаружено: {len(self.sports_discovered)}",
            f"Турниров успешно опрошено: {len(self.sports_queried)}",
            f"Турниров пропущено: {len(self.sports_skipped)}",
        ]
        if self.discovery_source == "fallback_hardcoded":
            lines.append(
                f"⚠️ Живой список видов спорта The Odds API недоступен "
                f"({self.discovery_error}) — использован резервный список из 7 крупных лиг."
            )
        if self.sports_skipped:
            skipped_preview = "; ".join(f"{k} — {v}" for k, v in list(self.sports_skipped.items())[:10])
            lines.append(f"Пропущенные турниры и причины: {skipped_preview}")
        lines.extend([
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
        ])
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


def _short_risk_note(candidate: ValueCandidate) -> str:
    """One short, plain-language risk line for the compact Telegram card
    -- same honest tone as _level_reason but written for a non-technical
    reader (no "consensus"/"outlier" jargon)."""
    if candidate.signal_level == "HIGH":
        return "Сильный сигнал: широкое согласие рынка, но результат матча не гарантирован."
    if candidate.signal_level == "MEDIUM":
        return "Средний сигнал: расхождение заметное, но требуется осторожность."
    return "Слабый сигнал: только для отслеживания, не для основной ставки."


def _render_telegram_card(candidate: ValueCandidate, index: int) -> str:
    """Compact Russian-language card for the '🤖 Прогнозы ИИ' button --
    only the fields required for a non-technical reader to understand one
    signal. No raw diagnostics, no English text, no internal jargon."""
    market_name = TELEGRAM_MARKET_LABELS.get(candidate.market_type, candidate.market_type)
    label = SIGNAL_LABELS.get(candidate.signal_level, candidate.signal_level)
    lines = [
        f"{label}",
        f"{candidate.home_team} — {candidate.away_team}"
        + (f" ({candidate.league})" if candidate.league else ""),
        f"🕒 {_fmt_local_time(candidate.match_datetime)}",
        f"Рынок: {market_name}",
        f"Выбор: {_selection_display_ru(candidate)}",
        f"Коэффициент: {candidate.best_price:.2f} ({candidate.best_bookmaker})",
        f"Справедливый коэффициент: {candidate.fair_price:.2f}",
        f"Расхождение: {_fmt_pct(candidate.edge)} | EV: {_fmt_pct(candidate.expected_value)}",
        f"Букмекеров: {candidate.unique_bookmaker_count}",
        _short_risk_note(candidate),
    ]
    if candidate.signal_level == "LOW":
        lines.append(_LOW_RISK_WARNING)
    return "\n".join([f"{index}. " + lines[0]] + lines[1:])


def render_telegram_signals_message(result: ValueSelectionResult, diagnostics: Diagnostics) -> List[str]:
    """Concise, Russian, non-technical message for the '🤖 Прогнозы ИИ'
    button: only the ranked signal cards (max MAX_TOTAL_SIGNALS, HIGH then
    MEDIUM then LOW) plus a short count summary. No raw diagnostics, HTTP
    error lists, skipped-competition lists, validation counts, duplicate
    counts, rejection-reason frequency lists, API internals, or English
    technical text -- all of that lives in render_value_report / /status
    instead. Returns a list of message chunks so a long signal list never
    exceeds Telegram's own message-length limit."""
    header = "🤖 AI Ставки — сигналы на ближайшие 36 часов"
    summary = (
        f"Итого: HIGH — {diagnostics.high_count}, MEDIUM — {diagnostics.medium_count}, "
        f"LOW — {diagnostics.low_count}, отклонено — {diagnostics.rejected_count}."
    )

    if not result.top_signals:
        body = "На ближайшие 36 часов подходящих сигналов не найдено."
        return [f"{header}\n\n{body}\n\n{summary}"]

    cards = [_render_telegram_card(c, i) for i, c in enumerate(result.top_signals, start=1)]

    chunks: List[str] = []
    current = header
    for card in cards:
        block = "\n\n" + card
        if len(current) + len(block) > TELEGRAM_MAX_CHARS and current != header:
            chunks.append(current.strip())
            current = ""
        current += block
    current += "\n\n" + summary
    chunks.append(current.strip())
    return chunks


def compute_top_rejection_reasons(rejected: List[ValueCandidate], limit: int = 10) -> List[str]:
    """Ranks rejection reasons by how often real candidates hit them --
    frequency, not alphabetical order, so the most common real blocker
    surfaces first."""
    counter: Counter = Counter()
    for candidate in rejected:
        for reason in candidate.rejection_reasons:
            counter[reason] += 1
    return [f"{reason} (x{count})" for reason, count in counter.most_common(limit)]


_HTTP_CODE_RE = re.compile(r"HTTP (\d+)")


def summarize_api_errors_ru(errors: List[str], sports_failed: Dict[str, str]) -> Optional[str]:
    """Collapses every per-competition API error into one short Russian
    line for /status (e.g. 'Некоторые турниры недоступны: HTTP 401 — 24
    турнира.') instead of a raw per-competition error dump. Returns None
    when there is nothing to report."""
    all_messages = list(errors) + list(sports_failed.values())
    if not all_messages:
        return None

    code_counts: Counter = Counter()
    other_count = 0
    for message in all_messages:
        match = _HTTP_CODE_RE.search(message)
        if match:
            code_counts[match.group(1)] += 1
        else:
            other_count += 1

    parts = [f"HTTP {code} — {count} турнира" for code, count in code_counts.most_common()]
    if other_count:
        parts.append(f"иная ошибка — {other_count} турнира")
    if not parts:
        return None
    return "Некоторые турниры недоступны: " + "; ".join(parts) + "."


def _render_rejected_candidate(candidate: ValueCandidate, index: int) -> str:
    market_name = MARKET_DISPLAY_NAMES.get(candidate.market_type, candidate.market_type)
    line_part = f" {candidate.line:+g}" if candidate.line is not None else ""
    reason = candidate.rejection_reasons[0] if candidate.rejection_reasons else "не указана"
    lines = [
        f"{index}. {candidate.league or candidate.sport}: "
        f"{candidate.home_team} — {candidate.away_team}",
        f"Рынок: {market_name}{line_part} | Исход: {candidate.selection}",
        f"Лучшая цена: {candidate.best_price:.2f} — {candidate.best_bookmaker} | "
        f"Справедливая цена: {candidate.fair_price:.2f}",
        f"Расхождение (edge): {_fmt_pct(candidate.edge)} | "
        f"Ожидаемая ценность (EV): {_fmt_pct(candidate.expected_value)} | "
        f"Букмекеров: {candidate.unique_bookmaker_count}",
        f"Причина отклонения: {reason}",
    ]
    return "\n".join(lines)


def render_value_report(result: ValueSelectionResult, diagnostics: Diagnostics) -> str:
    lines = ["AI Ставки — ранжированные рыночные сигналы (реальные коэффициенты, без статистики)\n"]
    lines.append(
        "Метод: сравнение реальных коэффициентов нескольких букмекеров по одному и тому же "
        "исходу, по всем активным футбольным турнирам The Odds API прямо сейчас — не только "
        "по крупным лигам. Никакая статистика команд не используется и не изобретается — только "
        "реальные цены. Это система ранжирования и исследования рынка, а не гарантия прибыли.\n"
    )
    lines.append(_NEVER_WORDS_NOTE)
    lines.append("")

    if not result.top_signals:
        lines.append("Сегодня нет сигналов ни одного уровня (HIGH/MEDIUM/LOW).\n")
        lines.append("Причины:")
        if diagnostics.events_in_window == 0:
            lines.append(
                f"- Нет ни одного события в ближайшие 36 часов (событий получено всего: "
                f"{diagnostics.events_received}, все исключены окном 36ч, "
                f"турниров опрошено: {len(diagnostics.sports_queried)}). Это не ошибка сопоставления "
                f"— рынок просто не предлагает матчей в этом окне прямо сейчас."
            )
        elif diagnostics.candidates_created == 0:
            lines.append("- Не найдено ни одного реального исхода с котировками нескольких букмекеров в событиях внутри окна 36 часов.")
        else:
            for reason in diagnostics.top_rejection_reasons[:8]:
                lines.append(f"- {reason}")
        lines.append("")

        if result.closest_rejected:
            lines.append(f"5 ближайших к порогу отклонённых кандидатов (реальные цены, ничего не выдумано):")
            for i, candidate in enumerate(result.closest_rejected, start=1):
                lines.append(_render_rejected_candidate(candidate, i))
                lines.append("")
    else:
        lines.append(
            f"Топ сигналов (до {len(result.top_signals)} из общего пула, приоритет HIGH \u2192 MEDIUM \u2192 LOW):"
        )
        lines.append("")
        for i, candidate in enumerate(result.top_signals, start=1):
            lines.append(_render_candidate(candidate, i))
            lines.append("")

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
