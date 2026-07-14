"""
Report rendering for the AI Betting Analytics module -- plain Russian
text, no HTML/Markdown assumptions so it is safe to send as a Telegram
message body as-is. Two levels:

- `compact_report` -- the public "📈 Статистика" button (short, no
  internal reasoning/technical detail).
- `admin_report`   -- the admin-only detailed breakdown (per league,
  market, signal level; 30d/90d/all-time; top/worst performers; trend).
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from analytics.config import DEFAULT_STAKE
from analytics.storage import AnalyticsStorage


def _fmt_pct(value: float) -> str:
    return f"{value:.1f}%"


def _fmt_money(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}"


def _since_days(now: datetime.datetime, days: int) -> str:
    return (now - datetime.timedelta(days=days)).isoformat()


def _best_worst(groups: List[Dict[str, Any]], *, min_settled: int = 3) -> "tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]":
    eligible = [g for g in groups if g["settled_predictions"] >= min_settled]
    if not eligible:
        return None, None
    best = max(eligible, key=lambda g: g["roi"])
    worst = min(eligible, key=lambda g: g["roi"])
    return best, worst


def compact_report(storage: AnalyticsStorage, *, stake: float = DEFAULT_STAKE, now: Optional[datetime.datetime] = None) -> str:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    overall = storage.overall_stats(stake=stake)
    signal_groups = storage.group_stats("signal_level", stake=stake)
    market_groups = storage.group_stats("market", stake=stake)
    league_groups = storage.group_stats("league", stake=stake)

    best_signal, worst_signal = _best_worst(signal_groups)
    best_market, _ = _best_worst(market_groups)
    best_league, _ = _best_worst(league_groups)

    lines = [
        "📈 Статистика AI Ставки",
        "",
        f"Всего прогнозов: {overall['total_predictions']}",
        f"Рассчитано: {overall['settled_predictions']}",
        f"Процент побед: {_fmt_pct(overall['win_rate'])}",
        f"ROI: {_fmt_pct(overall['roi'])}",
        f"Прибыль (при ставке {stake:g}): {_fmt_money(overall['profit'])}",
    ]
    if best_signal:
        lines.append(f"Лучший уровень сигнала: {best_signal['key']} (ROI {_fmt_pct(best_signal['roi'])})")
    if worst_signal and worst_signal is not best_signal:
        lines.append(f"Худший уровень сигнала: {worst_signal['key']} (ROI {_fmt_pct(worst_signal['roi'])})")
    if best_market:
        lines.append(f"Лучший рынок: {best_market['key']} (ROI {_fmt_pct(best_market['roi'])})")
    if best_league:
        lines.append(f"Лучшая лига: {best_league['key']} (ROI {_fmt_pct(best_league['roi'])})")
    lines.append(f"Обновлено: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    return "\n".join(lines)


def _trend_line(label: str, recent: Dict[str, Any], previous: Dict[str, Any]) -> str:
    if previous["settled_predictions"] == 0 or recent["settled_predictions"] == 0:
        return f"{label}: недостаточно данных для сравнения"
    roi_delta = recent["roi"] - previous["roi"]
    wr_delta = recent["win_rate"] - previous["win_rate"]
    direction = "улучшение" if roi_delta > 0 else ("ухудшение" if roi_delta < 0 else "без изменений")
    return (
        f"{label}: ROI {_fmt_pct(recent['roi'])} (было {_fmt_pct(previous['roi'])}, {direction}), "
        f"процент побед {_fmt_pct(recent['win_rate'])} (было {_fmt_pct(previous['win_rate'])}, "
        f"{'+' if wr_delta >= 0 else ''}{wr_delta:.1f} п.п.)"
    )


def admin_report(storage: AnalyticsStorage, *, stake: float = DEFAULT_STAKE, now: Optional[datetime.datetime] = None) -> str:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    overall = storage.overall_stats(stake=stake)
    last_30 = storage.overall_stats(stake=stake, since=_since_days(now, 30))
    last_90 = storage.overall_stats(stake=stake, since=_since_days(now, 90))
    prev_30_only = storage.overall_stats(
        stake=stake, since=_since_days(now, 60), until=_since_days(now, 30),
    )

    market_groups = sorted(storage.group_stats("market", stake=stake), key=lambda g: -g["roi"])
    league_groups = sorted(storage.group_stats("league", stake=stake), key=lambda g: -g["roi"])
    signal_groups = sorted(storage.group_stats("signal_level", stake=stake), key=lambda g: -g["roi"])

    lines = [
        "📊 Детальная статистика (админ)",
        "",
        "— Общее —",
        f"Всего прогнозов: {overall['total_predictions']} | Рассчитано: {overall['settled_predictions']}",
        f"Побед: {overall['wins']} | Поражений: {overall['losses']} | Возвратов: {overall['voids']} "
        f"(из них половинных побед: {overall['half_wins']}, половинных поражений: {overall['half_losses']})",
        f"Процент побед: {_fmt_pct(overall['win_rate'])} | ROI: {_fmt_pct(overall['roi'])} | "
        f"Прибыль: {_fmt_money(overall['profit'])} | Средний коэффициент: {overall['avg_odds']}",
        "",
        f"— Последние 30 дней — прогнозов: {last_30['total_predictions']}, ROI: {_fmt_pct(last_30['roi'])}, "
        f"процент побед: {_fmt_pct(last_30['win_rate'])}",
        f"— Последние 90 дней — прогнозов: {last_90['total_predictions']}, ROI: {_fmt_pct(last_90['roi'])}, "
        f"процент побед: {_fmt_pct(last_90['win_rate'])}",
        "",
        "— Тренд (последние 30 дней vs предыдущие 30 дней) —",
        _trend_line("30д", last_30, prev_30_only),
        "",
        "— По рынкам (сортировка по ROI) —",
    ]
    for g in market_groups[:10]:
        lines.append(f"  {g['key']}: {g['settled_predictions']} расч., ROI {_fmt_pct(g['roi'])}, побед {_fmt_pct(g['win_rate'])}")

    lines.append("")
    lines.append("— По лигам (сортировка по ROI) —")
    for g in league_groups[:10]:
        lines.append(f"  {g['key']}: {g['settled_predictions']} расч., ROI {_fmt_pct(g['roi'])}, побед {_fmt_pct(g['win_rate'])}")

    lines.append("")
    lines.append("— По уровню сигнала —")
    for g in signal_groups:
        lines.append(f"  {g['key']}: {g['settled_predictions']} расч., ROI {_fmt_pct(g['roi'])}, побед {_fmt_pct(g['win_rate'])}")

    lines.append("")
    lines.append(f"Обновлено: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    return "\n".join(lines)
