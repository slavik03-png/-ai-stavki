"""
Telegram rendering for the API-Football-only production v3 pipeline
(ai_predictions/football_pipeline.py). Deliberately separate from
ai_predictions/value_report.py (the older odds-driven report) -- the
card format, heading and no-signal text below are exact, spec-mandated
strings, not derived from the older report's layout.

Nothing here shows a bookmaker name, an edge/EV number, an internal
scoring formula, an API identifier, a raw rejection list, an HTTP error,
a raw market code (e.g. "1X"), a Python/module/pipeline name, a request
count, or any other technical detail -- see
tests/test_football_pipeline_v3.py and tests/test_prediction_card_format.py.
The full technical diagnostics remain reachable only via /status
(ai_predictions/football_pipeline.py's diagnostics dict + bot.py's
build_status_text()), never mixed into these user-facing messages.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ai_predictions.prediction_selector import RankedRecommendation
from ai_predictions.ru_translation import display_country_ru, league_ru, team_ru
from ai_predictions.value_config import SIGNAL_EMOJI_RU_CARD, SIGNAL_WORD_RU_CARD
from ai_predictions.window import format_card_time

#: This pipeline (ai_predictions/football_pipeline.py) is football-only --
#: see ai_predictions/__init__.py's module docstring. The label is kept as
#: a constant, not hardcoded inline, so a future sport-specific pipeline
#: only needs to pass its own label rather than change the card layout.
SPORT_LABEL_RU = "⚽ Футбол"

NO_SIGNAL_TEMPLATE = (
    "На ближайшие 36 часов найдено {count} матчей, но ни один вариант не достиг "
    "минимальной расчётной вероятности 56%. Слабые ставки бот не предлагает."
)

#: Shown when at least one candidate passed the probability threshold but
#: none of them had a real, matched bookmaker coefficient -- the bot never
#: shows a prediction without a confirmed real price, so these are
#: dropped rather than shown with a placeholder.
NO_ODDS_TEMPLATE = (
    "На ближайшие 36 часов найдено {count} вариантов с достаточной расчётной вероятностью, "
    "но ни один из них не найден в реальной линии букмекеров с подтверждённым коэффициентом. "
    "Бот не показывает прогнозы без реального коэффициента."
)

#: Shown when real fixtures were discovered but NONE of them currently has
#: a matched, real Odds API event at all -- the odds-first gate (see
#: football_pipeline.py) never even analyses such fixtures, so this is a
#: distinct, earlier reason than NO_ODDS_TEMPLATE (which is about a
#: specific market not being quoted for an already-matched event).
NO_ODDS_EVENTS_TEMPLATE = (
    "На ближайшие 36 часов найдено {count} матчей, но ни для одного из них сейчас нет "
    "реальных коэффициентов у букмекеров The Odds API. Бот анализирует только матчи, "
    "которые реально есть в линии букмекеров."
)

HEADING = "🤖 ПРОГНОЗЫ ИИ НА БЛИЖАЙШИЕ 36 ЧАСОВ"

#: Exact required text (per-user shown-tracking & pool re-selection,
#: 2026-07-15): shown whenever a specific Telegram user's re-selection
#: from the shared daily pool yields zero eligible picks -- whether
#: because every remaining real candidate has already been shown to THIS
#: user earlier today, or because none are left with enough lead time.
#: Sent standalone (no archive header, no generic no-signal template)
#: since it already explains the situation in full.
NOTHING_LEFT_FOR_USER_TEMPLATE = (
    "На текущий момент новых прогнозов из суточного пула не осталось. "
    "Утренний анализ сохранён, но все подходящие варианты уже были показаны или матчи начались. "
    "Новый пул будет сформирован после следующего суточного обновления."
)

#: Shown when today's pipeline run produced a pool with zero real
#: odds-backed recommendations -- a structurally different situation from
#: NOTHING_LEFT_FOR_USER (which requires that picks WERE available and
#: have since been shown/started).  Never says "варианты показаны или
#: начались" because no picks ever existed in this pool.
POOL_EMPTY_TEMPLATE = (
    "Сегодняшний пул прогнозов не сформирован: {reason}.\n"
    "Новые прогнозы появятся после следующего обновления квоты или источника данных."
)

#: Shown once, after all cards, whenever at least one recommendation was
#: produced -- never mixed into an individual card.
DISCLAIMER = (
    "ℹ️ Прогноз является аналитической оценкой, а не гарантией результата. "
    "Решение о ставке пользователь принимает самостоятельно."
)


def render_no_signal_message(found_fixtures: int) -> str:
    return NO_SIGNAL_TEMPLATE.format(count=found_fixtures)


def render_no_odds_message(candidates_without_odds: int) -> str:
    return NO_ODDS_TEMPLATE.format(count=candidates_without_odds)


def render_no_odds_events_message(found_fixtures: int) -> str:
    return NO_ODDS_EVENTS_TEMPLATE.format(count=found_fixtures)


def render_nothing_left_for_user_message() -> str:
    return NOTHING_LEFT_FOR_USER_TEMPLATE


def render_pool_empty_message(reason: str) -> str:
    """Shown when today's pipeline ran but produced ZERO real picks --
    structurally different from `render_nothing_left_for_user_message`
    (which requires that picks existed and have since been shown/started).
    Never implies any pick was ever shown or that a match began."""
    return POOL_EMPTY_TEMPLATE.format(reason=reason)


def _format_probability(probability: float) -> str:
    return f"{round(probability * 100)}%"


def _format_odds_ru(odds: float) -> str:
    """Russian decimal-comma formatting, e.g. 1,65. Always a real price by
    the time this is called -- callers only ever reach this with a
    recommendation that already has a confirmed real bookmaker price."""
    return f"{odds:.2f}".replace(".", ",")


def _basis_sentence_ru(source: str) -> str:
    """One plain-language sentence describing what the estimate is based
    on -- deliberately never mentions API-Football, caching, quotas, or
    any other implementation detail (that belongs only in /status)."""
    if source == "api_football_predictions":
        return "Оценка основана на аналитической модели с учётом текущей формы и состава команд."
    if source == "recent_form":
        return "Оценка основана на статистике побед и поражений команд в последних матчах."
    if source == "goal_model":
        return "Оценка основана на среднем количестве голов, забитых и пропущенных командами в последних матчах."
    # historical_baseline or any other/unknown source: honest about the
    # limitation without naming internal mechanisms.
    return "Точной статистики по этому матчу нет, поэтому оценка основана на общих футбольных показателях."


def _caution_sentence_ru(signal_level: str, sample_size_category: str) -> Optional[str]:
    """An optional second sentence, only when the confidence genuinely
    needs a caveat -- keeps HIGH-tier cards free of unnecessary hedging."""
    if sample_size_category == "none":
        return "Статистика по конкретному матчу ограничена, поэтому сигнал имеет низкий уровень."
    if signal_level == "LOW":
        return "Данных по матчу немного, поэтому сигнал имеет низкий уровень."
    if signal_level == "MEDIUM":
        return "Сигнал средней надёжности — стоит проявить осторожность."
    return None


def _short_explanation_ru(candidate, signal_level: str) -> str:
    """1-2 short, non-technical sentences for a regular user. Deliberately
    NOT `candidate.rationale` -- that internal field is written for
    tracking/settlement records and may mention API-Football, quota
    reserves, match counts, etc., none of which belong on a user-facing
    card (rules 8-11 of the card-formatting spec)."""
    parts = [_basis_sentence_ru(candidate.source)]
    caution = _caution_sentence_ru(signal_level, candidate.sample_size_category)
    if caution:
        parts.append(caution)
    return " ".join(parts)


def render_recommendation_card(
    index: int, recommendation: RankedRecommendation, odds: float, bookmaker: str,
) -> str:
    """Renders exactly one prediction as its own short, plain-Russian
    card. Kept as a standalone function (not inlined into the message
    builder) so it can be reused/tested independently.

    `odds` and `bookmaker` must both be real, confirmed values -- a
    recommendation without a real matched bookmaker price is never passed
    here; the caller drops it before rendering (see
    football_pipeline.run_football_predictions)."""
    c = recommendation.candidate
    fixture = c.fixture
    emoji = SIGNAL_EMOJI_RU_CARD[recommendation.signal_level]
    word = SIGNAL_WORD_RU_CARD[recommendation.signal_level]
    country = display_country_ru(fixture.league_country, fixture.league_name)
    league = league_ru(fixture.league_name) or "неизвестно"
    home = team_ru(fixture.home_team)
    away = team_ru(fixture.away_team)
    when = format_card_time(fixture.kickoff_utc)

    lines = [
        f"⚽ ПРОГНОЗ №{index}",
        "",
        f"Вид спорта: {SPORT_LABEL_RU}",
        f"🌍 Страна: {country}",
        f"🏆 Турнир: {league}",
        f"👥 Матч: {home} — {away}",
        f"🕒 Начало: {when}",
        f"🎯 Ставка: {c.market_label_ru}",
        f"📊 Расчётная вероятность: {_format_probability(c.probability)}",
        f"💰 Коэффициент: {_format_odds_ru(odds)} ({bookmaker})",
        f"{emoji} Уровень сигнала: {word}",
        "",
        "Краткое объяснение:",
        _short_explanation_ru(c, recommendation.signal_level),
    ]
    return "\n".join(lines)


def render_predictions_message(
    recommendations: List[RankedRecommendation],
    odds_by_fixture: Dict[int, Tuple[float, str]],
    *,
    found_fixtures: int,
    analysed_fixtures: int,
    candidates_without_odds: int = 0,
    matched_fixtures: Optional[int] = None,
) -> List[str]:
    """Returns one or more Telegram message chunks: a short heading (with
    Found/Analysed/Selected counts), one card per recommendation, and a
    closing disclaimer.

    `odds_by_fixture` maps fixture_id -> (real_price, real_bookmaker_title)
    and must already contain an entry for every recommendation passed in
    -- the caller is responsible for dropping any recommendation that has
    no real, matched bookmaker price before calling this function; this
    module never shows a placeholder coefficient.

    Three distinct empty-result reasons, checked in order (odds-first
    pipeline, football_pipeline.py):
    1. `candidates_without_odds > 0` -- some candidate cleared the
       probability threshold but its already-matched event doesn't quote
       that exact market right now.
    2. `matched_fixtures == 0` (and fixtures were found) -- no real Odds
       API event was matched to ANY discovered fixture at all, so nothing
       was even analysed.
    3. otherwise -- analysis ran but nothing cleared the probability
       threshold (the classic no-signal case)."""
    if not recommendations:
        if candidates_without_odds > 0:
            return [render_no_odds_message(candidates_without_odds)]
        if matched_fixtures == 0 and found_fixtures > 0:
            return [render_no_odds_events_message(found_fixtures)]
        return [render_no_signal_message(found_fixtures)]

    header = (
        f"{HEADING}\n\n"
        f"Найдено матчей: {found_fixtures}\n"
        f"Проанализировано: {analysed_fixtures}\n"
        f"Отобрано прогнозов: {len(recommendations)}"
    )
    cards = [
        render_recommendation_card(i + 1, rec, *odds_by_fixture[rec.candidate.fixture.fixture_id])
        for i, rec in enumerate(recommendations)
    ]
    return [header] + cards + [DISCLAIMER]
