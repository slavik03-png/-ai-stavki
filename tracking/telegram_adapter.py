"""
Telegram integration adapter -- Russian-ready text builders for future bot
buttons/commands. Intentionally isolated: nothing here is imported by
bot.py. It exists so that connecting these to the bot later is a matter of
wiring handlers, with no new formatting logic required.

Planned buttons:
    📊 Статистика прогнозов   -> statistics_summary_text
    🕓 Открытые прогнозы      -> open_predictions_text
    ✅ Последние результаты   -> recent_results_text
    📈 По рынкам              -> by_market_text
    🧠 По уровню уверенности  -> by_confidence_text
"""

from __future__ import annotations

import datetime
from typing import Sequence

from tracking import statistics as stats_mod
from tracking.models import STATUS_PENDING
from tracking.report import _fmt_num, _fmt_pct  # reuse the same formatting rules
from tracking.statistics import GRADED_STATUSES

BTN_STATISTICS = "📊 Статистика прогнозов"
BTN_OPEN = "🕓 Открытые прогнозы"
BTN_RECENT = "✅ Последние результаты"
BTN_BY_MARKET = "📈 По рынкам"
BTN_BY_CONFIDENCE = "🧠 По уровню уверенности"


def statistics_summary_text(predictions: Sequence) -> str:
    s = stats_mod.all_time(predictions)
    lines = [
        "📊 Статистика прогнозов",
        "",
        f"Всего: {s.total} | Рассчитано: {s.settled} | В ожидании: {s.pending}",
        f"Выиграно: {s.won} | Проиграно: {s.lost} | Возврат: {s.returned}",
        f"Win rate: {_fmt_pct(s.win_rate)}",
        f"ROI: {_fmt_pct(s.roi)}",
        f"Прибыль: {_fmt_num(s.flat_stake_profit, 2)} юнит(ов)",
    ]
    if s.sample_too_small:
        lines.append("⚠️ Выборка пока слишком мала для надёжных выводов.")
    return "\n".join(lines)


def open_predictions_text(predictions: Sequence, limit: int = 10) -> str:
    pending = [p for p in predictions if p["status"] == STATUS_PENDING]
    lines = [f"🕓 Открытые прогнозы ({len(pending)})", ""]
    if not pending:
        lines.append("Открытых прогнозов нет.")
        return "\n".join(lines)
    for p in pending[:limit]:
        lines.append(
            f"{p['home_team']} — {p['away_team']}\n"
            f"  {p['market_name']} | коэф. {p['bookmaker_odds']} | уверенность {p['confidence_score']}%"
        )
    if len(pending) > limit:
        lines.append(f"... и ещё {len(pending) - limit}")
    return "\n".join(lines)


def recent_results_text(predictions: Sequence, limit: int = 10) -> str:
    settled = sorted(
        (p for p in predictions if p["status"] in GRADED_STATUSES),
        key=lambda p: p["settled_at"] or "",
        reverse=True,
    )
    lines = ["✅ Последние результаты", ""]
    if not settled:
        lines.append("Пока нет рассчитанных прогнозов.")
        return "\n".join(lines)
    status_ru = {
        "won": "✅ выигрыш", "lost": "❌ проигрыш", "returned": "↩️ возврат",
        "half_won": "🟢 частичный выигрыш", "half_lost": "🔴 частичный проигрыш",
        "cancelled": "🚫 отменено", "void": "⚪ аннулировано",
    }
    for p in settled[:limit]:
        lines.append(
            f"{p['home_team']} — {p['away_team']}: {p['market_name']} — "
            f"{status_ru.get(p['status'], p['status'])} (счёт {p['final_score'] or '—'})"
        )
    return "\n".join(lines)


def by_market_text(predictions: Sequence) -> str:
    breakdown = stats_mod.by_market_type(predictions)
    lines = [BTN_BY_MARKET, ""]
    if not breakdown:
        lines.append("Нет данных.")
        return "\n".join(lines)
    for market, s in sorted(breakdown.items(), key=lambda kv: kv[1].settled, reverse=True):
        lines.append(f"{market}: рассчитано {s.settled}, win rate {_fmt_pct(s.win_rate)}, ROI {_fmt_pct(s.roi)}")
    return "\n".join(lines)


def by_confidence_text(predictions: Sequence) -> str:
    breakdown = stats_mod.by_confidence_level(predictions)
    lines = [BTN_BY_CONFIDENCE, ""]
    if not breakdown:
        lines.append("Нет данных.")
        return "\n".join(lines)
    for level, s in sorted(breakdown.items(), key=lambda kv: kv[1].settled, reverse=True):
        lines.append(f"{level}: рассчитано {s.settled}, win rate {_fmt_pct(s.win_rate)}, ROI {_fmt_pct(s.roi)}")
    return "\n".join(lines)
