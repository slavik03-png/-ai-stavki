"""
Provider-agnostic Russian-language raw-data report renderer.

Works against any `football.interface.FootballStatisticsProvider`
implementation -- it never imports a concrete provider. Shows collected
statistics only; no betting recommendations. Missing data is always shown
with its reason, never invented.
"""

from __future__ import annotations

from typing import List, Optional

from football.interface import FootballStatisticsProvider, Stat


def _fmt(stat: Stat, label: str, formatter=None) -> str:
    if not stat.available:
        return f"{label}: {stat.reason}"
    return f"{label}: {formatter(stat.value) if formatter else stat.value}"


def _team_section(provider: FootballStatisticsProvider, team: str, count: int = 10) -> List[str]:
    lines = [f"\n=== {team} ==="]

    form = provider.get_home_away_form(team, count)
    if form.available:
        v = form.value
        lines.append(f"Форма (последние {v.matches_counted} матчей): {v.overall or '—'}")
        lines.append(f"Домашняя форма: {v.home or '—'} | Гостевая форма: {v.away or '—'}")
    else:
        lines.append(f"Форма: {form.reason}")

    goals_half = provider.get_goals_by_half(team, count)
    if goals_half.available:
        v = goals_half.value
        lines.append(
            f"Голы по таймам (ср. за {v.matches_counted} матчей): "
            f"1-й тайм {v.first_half_scored_avg}/{v.first_half_conceded_avg} (забито/пропущено), "
            f"2-й тайм {v.second_half_scored_avg}/{v.second_half_conceded_avg}"
        )
        if v.intervals:
            lines.append(f"  По интервалам: {v.intervals}")
        else:
            lines.append(f"  По 15-минутным интервалам: {v.intervals_reason or 'нет данных'}")
    else:
        lines.append(f"Голы по таймам: {goals_half.reason}")

    btts = provider.get_btts_frequency(team, count)
    lines.append(_fmt(btts, "Обе забьют (BTTS)"))

    clean = provider.get_clean_sheets(team, count)
    if clean.available:
        v = clean.value
        lines.append(f"Сухие матчи: {v.clean_sheets}/{v.matches_counted} | Не забили: {v.failed_to_score}/{v.matches_counted}")
    else:
        lines.append(f"Сухие матчи / не забили: {clean.reason}")

    corners = provider.get_corners(team, count)
    lines.append(_fmt(corners, "Угловые (ср. за матч)", lambda v: f"{v.average} ({v.matches_counted} матчей)"))

    fouls = provider.get_fouls(team, count)
    lines.append(_fmt(fouls, "Фолы (ср. за матч)", lambda v: f"{v.average} ({v.matches_counted} матчей)"))

    cards = provider.get_cards(team, count)
    lines.append(_fmt(cards, "Карточки (ср. за матч)", lambda v: f"жёлтые {v.avg_yellow}, красные {v.avg_red}"))

    shots = provider.get_shots(team, count)
    lines.append(_fmt(shots, "Удары (ср. за матч)", lambda v: f"всего {v.avg_total}, в створ {v.avg_on_target}"))

    injuries = provider.get_injuries(team)
    if injuries.available:
        players = injuries.value
        lines.append(
            "Травмы/отсутствующие: "
            + (", ".join(f"{p.player} ({p.reason})" if p.reason else p.player for p in players) if players
               else "нет данных о травмированных игроках")
        )
    else:
        lines.append(f"Травмы/отсутствующие: {injuries.reason}")

    return lines


def render_report_ru(
    provider: FootballStatisticsProvider,
    home_team: str,
    away_team: str,
    league: Optional[str] = None,
    count: int = 10,
) -> str:
    lines = [f"🏟 {home_team} — {away_team}", f"Источник данных: {provider.name}"]

    for team in (home_team, away_team):
        lines.extend(_team_section(provider, team, count))

    lines.append("\n=== Личные встречи (H2H) ===")
    h2h = provider.get_head_to_head(home_team, away_team, count)
    if h2h.available:
        for match in h2h.value:
            lines.append(
                f"  {match.date}: {match.home_team} {match.home_goals}-{match.away_goals} {match.away_team}"
            )
    else:
        lines.append(f"  {h2h.reason}")

    lines.append("\n=== Составы ===")
    lineups = provider.get_lineups(home_team, away_team)
    if lineups.available:
        for lineup in lineups.value:
            starters = ", ".join(p.name for p in lineup.starters)
            lines.append(f"  {lineup.team} ({lineup.formation}): {starters}")
    else:
        lines.append(f"  {lineups.reason}")

    if league:
        lines.append(f"\n=== Турнирная таблица: {league} ===")
        standings = provider.get_standings(league)
        if standings.available:
            for row in standings.value:
                if row.team in (home_team, away_team):
                    lines.append(f"  {row.team}: место {row.rank}, очки {row.points}, игр {row.played}")
        else:
            lines.append(f"  {standings.reason}")

    return "\n".join(lines)
