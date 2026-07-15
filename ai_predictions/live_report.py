"""
Telegram rendering for Live in-play mode (2026-07-15). Deliberately its
own module -- never reuses prediction_report.py's or value_report.py's
templates, since Live cards need live-specific fields (current minute,
current score) that neither pre-match report has, and this feature must
never render candidate.rationale or any other internal/technical field
directly on a user card (same rule as prediction_report.py).
"""

from __future__ import annotations

from typing import Dict, List

from ai_predictions.live_candidates import LiveCandidate
from ai_predictions.ru_translation import display_country_ru, league_ru, team_ru
from ai_predictions.value_config import SIGNAL_EMOJI_RU_CARD, SIGNAL_WORD_RU_CARD
from ai_predictions.value_report import MARKET_DISPLAY_NAMES, _selection_display_ru

HEADING = "🔴 LIVE — ПРОГНОЗЫ ПО МАТЧАМ, ИДУЩИМ ПРЯМО СЕЙЧАС"

NO_LIVE_MATCHES_TEXT = (
    "Сейчас нет матчей, идущих в прямом эфире, по данным API-Football."
)

NO_MATCHED_ODDS_TEMPLATE = (
    "Сейчас идёт {count} матчей, но ни для одного из них не найдены реальные "
    "коэффициенты букмекеров The Odds API. Бот анализирует только матчи, которые "
    "реально есть в живой линии букмекеров."
)

NO_SIGNAL_TEMPLATE = (
    "Сейчас идёт {count} матчей с реальными коэффициентами, но ни один вариант не "
    "показал достаточного расхождения между букмекерами. Слабые Live-сигналы бот не предлагает."
)

DISCLAIMER = (
    "ℹ️ Live-прогноз — аналитическая оценка по текущим коэффициентам, а не гарантия результата. "
    "Ставки по ходу матча особенно рискованны: минута и счёт могут поменять ситуацию мгновенно."
)


def render_no_live_matches_message() -> str:
    return NO_LIVE_MATCHES_TEXT


def render_no_matched_odds_message(live_count: int) -> str:
    return NO_MATCHED_ODDS_TEMPLATE.format(count=live_count)


def render_no_signal_message(matched_count: int) -> str:
    return NO_SIGNAL_TEMPLATE.format(count=matched_count)


def _score_text(live_fixture) -> str:
    home = live_fixture.home_score if live_fixture.home_score is not None else "?"
    away = live_fixture.away_score if live_fixture.away_score is not None else "?"
    return f"{home}:{away}"


def _minute_text(live_fixture) -> str:
    status = live_fixture.status_short
    if status == "HT":
        return "перерыв"
    if live_fixture.elapsed_minutes is not None:
        return f"{live_fixture.elapsed_minutes}'"
    return status or "идёт"


def render_live_card(index: int, live_candidate: LiveCandidate) -> str:
    lf = live_candidate.live_fixture
    c = live_candidate.value_candidate
    emoji = SIGNAL_EMOJI_RU_CARD[c.signal_level]
    word = SIGNAL_WORD_RU_CARD[c.signal_level]
    country = display_country_ru(lf.league_country, lf.league_name)
    league = league_ru(lf.league_name) or "неизвестно"
    home = team_ru(lf.home_team)
    away = team_ru(lf.away_team)

    lines = [
        f"🔴 LIVE №{index}",
        "",
        f"🌍 Страна: {country}",
        f"🏆 Турнир: {league}",
        f"👥 Матч: {home} — {away}",
        f"⏱ Минута: {_minute_text(lf)}  |  Счёт: {_score_text(lf)}",
        f"🎯 Ставка: {MARKET_DISPLAY_NAMES.get(c.market_type, c.market_type)} — {_selection_display_ru(c)}",
        f"💰 Коэффициент: {c.best_price:.2f} ({c.best_bookmaker})",
        f"{emoji} Уровень сигнала: {word}",
        "",
        "Краткое объяснение:",
        "Оценка основана на сравнении текущих коэффициентов нескольких букмекеров прямо по ходу матча.",
    ]
    return "\n".join(lines)


def render_live_message(
    live_candidates: List[LiveCandidate], *, live_fixture_count: int, matched_fixture_count: int,
) -> List[str]:
    """Returns Telegram message chunks: heading + counts, one card per
    candidate, then the Live-specific disclaimer. Empty-result reasons
    checked in order: no live fixtures at all -> fixtures live but none
    matched to real odds -> matched but no signal cleared the bar."""
    if not live_candidates:
        if live_fixture_count == 0:
            return [render_no_live_matches_message()]
        if matched_fixture_count == 0:
            return [render_no_matched_odds_message(live_fixture_count)]
        return [render_no_signal_message(matched_fixture_count)]

    header = (
        f"{HEADING}\n\n"
        f"Матчей идёт сейчас: {live_fixture_count}\n"
        f"Сопоставлено с реальными коэффициентами: {matched_fixture_count}\n"
        f"Отобрано Live-сигналов: {len(live_candidates)}"
    )
    cards = [render_live_card(i + 1, lc) for i, lc in enumerate(live_candidates)]
    return [header] + cards + [DISCLAIMER]
