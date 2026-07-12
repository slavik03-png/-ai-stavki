"""
Russian-language analytical report generator.

Combines football.prediction.MatchContext / MarketResult and
football.recommendation.RecommendationReport into a structured, readable
report. Shows collected statistics and their derived confidence only --
never invents data, and never claims a recommendation is guaranteed,
certain, safe, or 100%.
"""

from __future__ import annotations

from typing import List

from football.prediction import MatchContext, MarketResult, Stat
from football.recommendation import RecommendationReport, confidence_band_label

FINAL_DISCLAIMER = "Прогноз основан на доступной статистике и не гарантирует выигрыш ставки."

_STARS_CHAR = "⭐"


def _stars(n: int) -> str:
    return _STARS_CHAR * n


def _fmt_stat_line(label: str, stat: Stat, formatter=None) -> str:
    if not stat.available:
        return f"{label}: нет данных ({stat.reason})"
    return f"{label}: {formatter(stat.value) if formatter else stat.value}"


def _section_quality(ctx: MatchContext, markets: List[MarketResult]) -> List[str]:
    lines = ["## 2. Качество и полнота данных"]
    unavailable = [m for m in markets if m.status == "unavailable"]
    weak = [m for m in markets if m.status == "weak"]
    lines.append(f"Источник данных: {ctx.provider_name}")
    lines.append(f"Проанализировано рынков: {len(markets)}")
    lines.append(f"Недоступно из-за отсутствия статистики: {len(unavailable)}")
    lines.append(f"Слабых по уверенности: {len(weak)}")
    gaps = []
    for label, stat in (
        ("составы", ctx.lineups),
        (f"травмы {ctx.home_team}", ctx.home_injuries),
        (f"травмы {ctx.away_team}", ctx.away_injuries),
        ("личные встречи", ctx.h2h),
        ("турнирная таблица", ctx.standings),
    ):
        if not stat.available:
            gaps.append(f"{label} — {stat.reason}")
    if gaps:
        lines.append("Отсутствующие исходные данные:")
        for g in gaps:
            lines.append(f"  - {g}")
    else:
        lines.append("Все базовые источники данных доступны.")
    return lines


def _section_form(ctx: MatchContext) -> List[str]:
    lines = ["## 3. Форма команд"]
    for team, form in ((ctx.home_team, ctx.home_form), (ctx.away_team, ctx.away_form)):
        if form.available:
            lines.append(f"{team}: общая форма {form.value.overall or '—'} (по {form.value.matches_counted} матчам)")
        else:
            lines.append(f"{team}: форма недоступна — {form.reason}")
    return lines


def _section_home_away_form(ctx: MatchContext) -> List[str]:
    lines = ["## 4. Домашняя и выездная форма"]
    if ctx.home_form.available:
        lines.append(f"{ctx.home_team} дома: {ctx.home_form.value.home or '—'}")
    else:
        lines.append(f"{ctx.home_team} дома: нет данных — {ctx.home_form.reason}")
    if ctx.away_form.available:
        lines.append(f"{ctx.away_team} в гостях: {ctx.away_form.value.away or '—'}")
    else:
        lines.append(f"{ctx.away_team} в гостях: нет данных — {ctx.away_form.reason}")
    return lines


def _section_h2h(ctx: MatchContext) -> List[str]:
    lines = ["## 5. Личные встречи"]
    if ctx.h2h.available and ctx.h2h.value:
        for m in ctx.h2h.value:
            lines.append(f"  {m.date}: {m.home_team} {m.home_goals}-{m.away_goals} {m.away_team}")
        lines.append("Личные встречи учитываются только как второстепенный фактор, не как основной.")
    else:
        reason = ctx.h2h.reason if ctx.h2h.available else ctx.h2h.reason
        lines.append(f"Нет данных личных встреч: {reason}")
    return lines


def _market_lookup(markets: List[MarketResult], *names: str) -> List[MarketResult]:
    return [m for m in markets if m.market_name in names]


def _section_from_markets(title: str, markets: List[MarketResult]) -> List[str]:
    lines = [title]
    if not markets:
        lines.append("  Нет данных по этому разделу.")
        return lines
    for m in markets:
        if m.status == "unavailable":
            lines.append(f"  {m.market_name}: нет данных ({'; '.join(m.missing_statistics) or 'недостаточно статистики'})")
        else:
            lines.append(f"  {m.market_name}: уверенность {m.confidence}% {_stars(m.stars)} (риск: {m.risk})")
            for e in m.explanation:
                lines.append(f"    - {e}")
    return lines


def _section_additional(title: str, markets: List[MarketResult], family_prefix: str) -> List[str]:
    filtered = [m for m in markets if m.family.startswith(family_prefix)]
    return _section_from_markets(title, filtered)


def _section_standings(ctx: MatchContext) -> List[str]:
    lines = ["## 14. Турнирное положение"]
    if ctx.standings.available:
        for row in ctx.standings.value:
            if row.team in (ctx.home_team, ctx.away_team):
                lines.append(f"  {row.team}: место {row.rank}, очки {row.points}, игр {row.played}")
    else:
        lines.append(f"  Нет данных турнирной таблицы: {ctx.standings.reason}")
    return lines


def _section_lineups_injuries(ctx: MatchContext) -> List[str]:
    lines = ["## 15. Составы, травмы и отсутствующие игроки"]
    if ctx.lineups.available:
        for lineup in ctx.lineups.value:
            starters = ", ".join(p.name for p in lineup.starters)
            lines.append(f"  Состав {lineup.team} ({lineup.formation}): {starters}")
    else:
        lines.append(f"  Составы: нет данных — {ctx.lineups.reason}")

    for team, injuries in ((ctx.home_team, ctx.home_injuries), (ctx.away_team, ctx.away_injuries)):
        if injuries.available:
            if injuries.value:
                names = ", ".join(f"{p.player} ({p.reason})" if p.reason else p.player for p in injuries.value)
                lines.append(f"  Травмы {team}: {names}")
            else:
                lines.append(f"  Травмы {team}: нет травмированных игроков по имеющимся данным")
        else:
            lines.append(f"  Травмы {team}: нет данных — {injuries.reason}")
    return lines


def _render_recommendation_block(title: str, market: MarketResult) -> List[str]:
    lines = [title]
    lines.append(market.market_name)
    lines.append(f"Уверенность: {market.confidence}% ({confidence_band_label(market.confidence)})")
    lines.append(f"Оценка: {_stars(market.stars)}")
    lines.append(f"Риск: {market.risk}")
    if market.explanation:
        lines.append("")
        lines.append("Почему:")
        for e in market.explanation:
            lines.append(f"- {e}")
    if market.missing_statistics:
        lines.append("")
        lines.append("Что снижает уверенность:")
        for e in market.missing_statistics:
            lines.append(f"- {e}")
    return lines


def render_report_ru(
    ctx: MatchContext,
    markets: List[MarketResult],
    recommendation: RecommendationReport,
) -> str:
    lines: List[str] = []

    lines.append(f"# 1. Матч")
    lines.append(f"{ctx.home_team} — {ctx.away_team}" + (f" ({ctx.league})" if ctx.league else ""))
    lines.append("")

    lines.extend(_section_quality(ctx, markets))
    lines.append("")
    lines.extend(_section_form(ctx))
    lines.append("")
    lines.extend(_section_home_away_form(ctx))
    lines.append("")
    lines.extend(_section_h2h(ctx))
    lines.append("")

    lines.extend(_section_from_markets(
        "## 6. Голы",
        _market_lookup(markets, "Тотал больше 0.5", "Тотал больше 1.5", "Тотал больше 2.5",
                        "Тотал больше 3.5", "Тотал меньше 2.5"),
    ))
    lines.append("")
    lines.extend(_section_from_markets("## 7. Обе забьют", _market_lookup(markets, "Обе забьют — Да", "Обе забьют — Нет")))
    lines.append("")
    lines.extend(_section_from_markets(
        "## 8. Первый тайм",
        [m for m in markets if m.market_type == "first_half"],
    ))
    lines.append("")
    lines.extend(_section_from_markets(
        "## 9. Второй тайм",
        [m for m in markets if m.market_type == "second_half"],
    ))
    lines.append("")
    lines.extend(_section_additional("## 10. Угловые", markets, "corners"))
    lines.append("")
    lines.extend(_section_additional("## 11. Карточки", markets, "cards"))
    lines.append("")
    lines.extend(_section_additional("## 12. Фолы", markets, "fouls"))
    lines.append("")
    lines.extend(_section_additional("## 13. Удары и удары в створ", markets, "shots"))
    lines.append("")
    lines.extend(_section_standings(ctx))
    lines.append("")
    lines.extend(_section_lineups_injuries(ctx))
    lines.append("")

    lines.append("## 16. Основная рекомендация")
    if recommendation.main is None:
        lines.append(f"{recommendation.message}.")
        lines.append("Имеющихся статистических данных недостаточно для уверенной рекомендации по этому матчу.")
    else:
        lines.extend(_render_recommendation_block("Основная рекомендация:", recommendation.main))
    lines.append("")

    lines.append("## 17. Альтернативные варианты")
    if recommendation.alternatives:
        for m in recommendation.alternatives:
            lines.extend(_render_recommendation_block("Альтернатива:", m))
            lines.append("")
    else:
        lines.append("Нет альтернативных вариантов с достаточной уверенностью.")
        lines.append("")

    lines.append("## 18. Рискованные варианты")
    if recommendation.high_risk:
        for m in recommendation.high_risk:
            lines.extend(_render_recommendation_block("Рискованный вариант (не гарантирован):", m))
            lines.append("")
    else:
        lines.append("Рискованных вариантов, достойных упоминания, не выявлено.")
        lines.append("")

    lines.append("## 19. Рынки, которые лучше пропустить")
    if recommendation.avoid:
        for m in recommendation.avoid:
            lines.append(f"- {m.market_name}: уверенность {m.confidence}% — недостаточно оснований для ставки")
    else:
        lines.append("Явных рынков для избегания не выявлено, но общая осторожность рекомендуется.")
    lines.append("")

    lines.append("## 20. Итоговое предупреждение")
    if recommendation.no_reliable_recommendation:
        lines.append("Надёжная рекомендация по этому матчу отсутствует — статистики недостаточно.")
    lines.append(FINAL_DISCLAIMER)

    return "\n".join(lines)
