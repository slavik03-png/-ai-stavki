"""
Russian-language performance report over tracked predictions.

Never claims profitability is guaranteed. Warns explicitly when the
decisive-result sample is too small (below `statistics.MIN_RELIABLE_SAMPLE`)
to draw conclusions from.
"""

from __future__ import annotations

import datetime
from typing import List, Sequence

from tracking import statistics as stats_mod
from tracking.models import STATUS_PENDING
from tracking.statistics import MIN_RELIABLE_SAMPLE, Stats

FINAL_DISCLAIMER = (
    "Статистика отражает прошлые результаты и не гарантирует прибыль или "
    "победу в будущих прогнозах."
)


def _fmt_pct(value) -> str:
    return "нет данных" if value is None else f"{value:.2f}%"


def _fmt_num(value, digits: int = 2) -> str:
    return "нет данных" if value is None else f"{value:.{digits}f}"


def _overall_block(title: str, s: Stats) -> List[str]:
    lines = [title]
    lines.append(f"Всего прогнозов: {s.total}")
    lines.append(f"Рассчитано: {s.settled}, в ожидании: {s.pending}")
    lines.append(
        f"Выиграно: {s.won}, проиграно: {s.lost}, возврат: {s.returned}, "
        f"частично выиграно: {s.half_won}, частично проиграно: {s.half_lost}"
    )
    lines.append(f"Отменено: {s.cancelled}, не рассчитано (нет данных): {s.unresolved}, перенесено: {s.postponed}")
    lines.append(f"Win rate (без учёта возвратов и отмен): {_fmt_pct(s.win_rate)}")
    lines.append(f"Success rate (с учётом частичных исходов): {_fmt_pct(s.success_rate)}")
    lines.append(f"Средний коэффициент: {_fmt_num(s.average_odds, 3)}")
    lines.append(f"Прибыль в юнитах (ставка 1 юнит): {_fmt_num(s.flat_stake_profit, 3)}")
    lines.append(f"ROI: {_fmt_pct(s.roi)}")
    lines.append(f"Самая длинная победная серия: {s.longest_winning_streak}")
    lines.append(f"Самая длинная проигрышная серия: {s.longest_losing_streak}")
    if s.sample_too_small:
        lines.append(
            f"⚠️ Выборка слишком мала (< {MIN_RELIABLE_SAMPLE} рассчитанных ставок) — "
            "статистике пока нельзя доверять как долгосрочной тенденции."
        )
    return lines


def _breakdown_block(title: str, breakdown) -> List[str]:
    lines = [title]
    if not breakdown:
        lines.append("  Нет данных для этого разреза.")
        return lines
    # Sort by number of settled predictions, most active first.
    for key, s in sorted(breakdown.items(), key=lambda kv: kv[1].settled, reverse=True):
        lines.append(
            f"  {key}: рассчитано {s.settled}, win rate {_fmt_pct(s.win_rate)}, "
            f"ROI {_fmt_pct(s.roi)}, прибыль {_fmt_num(s.flat_stake_profit, 2)} юнит(ов)"
        )
    return lines


def _strongest_weakest_markets(by_market: dict, min_settled: int = 3) -> "tuple[list, list]":
    eligible = [(name, s) for name, s in by_market.items() if s.settled >= min_settled and s.roi is not None]
    strongest = sorted(eligible, key=lambda kv: kv[1].roi, reverse=True)[:3]
    weakest = sorted(eligible, key=lambda kv: kv[1].roi)[:3]
    return strongest, weakest


def render_report_ru(
    predictions: Sequence,
    now: "datetime.datetime | None" = None,
) -> str:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    overall = stats_mod.all_time(predictions)
    pending_predictions = [p for p in predictions if p["status"] == STATUS_PENDING]

    by_market = stats_mod.by_market_type(predictions)
    by_confidence = stats_mod.by_confidence_level(predictions)
    by_group = stats_mod.by_recommendation_group(predictions)
    last_7 = stats_mod.last_n_days(predictions, 7, now)
    last_30 = stats_mod.last_n_days(predictions, 30, now)

    strongest, weakest = _strongest_weakest_markets(by_market)

    lines: List[str] = []
    lines.append("# Статистика прогнозов AI Ставки")
    lines.append("")
    lines.extend(_overall_block("## 1. Общие результаты", overall))
    lines.append("")

    lines.append(f"## 2. Открытые прогнозы ({len(pending_predictions)})")
    if pending_predictions:
        for p in pending_predictions[:10]:
            lines.append(
                f"  {p['home_team']} — {p['away_team']}: {p['market_name']} "
                f"(коэф. {p['bookmaker_odds']}, уверенность {p['confidence_score']}%)"
            )
        if len(pending_predictions) > 10:
            lines.append(f"  ... и ещё {len(pending_predictions) - 10}")
    else:
        lines.append("  Открытых прогнозов нет.")
    lines.append("")

    lines.append("## 3. Сильнейшие рынки (по ROI)")
    if strongest:
        for name, s in strongest:
            lines.append(f"  {name}: ROI {_fmt_pct(s.roi)}, win rate {_fmt_pct(s.win_rate)} ({s.settled} ставок)")
    else:
        lines.append("  Недостаточно рассчитанных ставок для выделения сильнейших рынков.")
    lines.append("")

    lines.append("## 4. Слабейшие рынки (по ROI)")
    if weakest:
        for name, s in weakest:
            lines.append(f"  {name}: ROI {_fmt_pct(s.roi)}, win rate {_fmt_pct(s.win_rate)} ({s.settled} ставок)")
    else:
        lines.append("  Недостаточно рассчитанных ставок для выделения слабейших рынков.")
    lines.append("")

    lines.extend(_breakdown_block("## 5. Результаты по уровню уверенности", by_confidence))
    lines.append("")
    lines.extend(_breakdown_block("## 6. Результаты по группе рекомендаций", by_group))
    lines.append("")

    lines.extend(_overall_block("## 7. Последние 7 дней", last_7))
    lines.append("")
    lines.extend(_overall_block("## 8. Последние 30 дней", last_30))
    lines.append("")

    lines.append("## 9. Итоговое предупреждение")
    lines.append(FINAL_DISCLAIMER)

    return "\n".join(lines)
