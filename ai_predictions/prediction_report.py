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

from typing import List, Optional

from ai_predictions.prediction_selector import RankedRecommendation
from ai_predictions.ru_translation import country_ru, league_ru, team_ru
from ai_predictions.value_config import SIGNAL_EMOJI_RU_CARD, SIGNAL_WORD_RU_CARD
from ai_predictions.window import format_card_time

NO_SIGNAL_TEMPLATE = (
    "На ближайшие 36 часов найдено {count} матчей, но ни один вариант не достиг "
    "минимальной расчётной вероятности 56%. Слабые ставки бот не предлагает."
)

HEADING = "🤖 ПРОГНОЗЫ ИИ НА БЛИЖАЙШИЕ 36 ЧАСОВ"

#: Shown once, after all cards, whenever at least one recommendation was
#: produced -- never mixed into an individual card.
DISCLAIMER = (
    "ℹ️ Прогноз является аналитической оценкой, а не гарантией результата. "
    "Решение о ставке пользователь принимает самостоятельно."
)


def render_no_signal_message(found_fixtures: int) -> str:
    return NO_SIGNAL_TEMPLATE.format(count=found_fixtures)


def _format_probability(probability: float) -> str:
    return f"{round(probability * 100)}%"


def _format_odds_ru(odds: Optional[float]) -> str:
    """Russian decimal-comma formatting, e.g. 1,65 -- or the honest
    'нет данных' when no real coefficient was found (never fabricated,
    never a bookmaker name)."""
    if odds is None:
        return "нет данных"
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


def render_recommendation_card(index: int, recommendation: RankedRecommendation, odds: Optional[float]) -> str:
    """Renders exactly one prediction as its own short, plain-Russian
    card. Kept as a standalone function (not inlined into the message
    builder) so it can be reused/tested independently."""
    c = recommendation.candidate
    fixture = c.fixture
    emoji = SIGNAL_EMOJI_RU_CARD[recommendation.signal_level]
    word = SIGNAL_WORD_RU_CARD[recommendation.signal_level]
    country = country_ru(fixture.league_country) or "неизвестно"
    league = league_ru(fixture.league_name) or "неизвестно"
    home = team_ru(fixture.home_team)
    away = team_ru(fixture.away_team)
    when = format_card_time(fixture.kickoff_utc)

    lines = [
        f"⚽ ПРОГНОЗ №{index}",
        "",
        f"🌍 Страна: {country}",
        f"🏆 Турнир: {league}",
        f"👥 Матч: {home} — {away}",
        f"🕒 Начало: {when}",
        f"🎯 Ставка: {c.market_label_ru}",
        f"📊 Расчётная вероятность: {_format_probability(c.probability)}",
        f"💰 Ориентировочный коэффициент: {_format_odds_ru(odds)}",
        f"{emoji} Уровень сигнала: {word}",
        "",
        "Краткое объяснение:",
        _short_explanation_ru(c, recommendation.signal_level),
    ]
    return "\n".join(lines)


def render_predictions_message(
    recommendations: List[RankedRecommendation],
    odds_by_fixture: dict,
    *,
    found_fixtures: int,
    analysed_fixtures: int,
) -> List[str]:
    """Returns one or more Telegram message chunks: a short heading (with
    Found/Analysed/Selected counts), one card per recommendation, and a
    closing disclaimer -- or the exact no-signal message alone if
    `recommendations` is empty."""
    if not recommendations:
        return [render_no_signal_message(found_fixtures)]

    header = (
        f"{HEADING}\n\n"
        f"Найдено матчей: {found_fixtures}\n"
        f"Проанализировано: {analysed_fixtures}\n"
        f"Отобрано прогнозов: {len(recommendations)}"
    )
    cards = [
        render_recommendation_card(i + 1, rec, odds_by_fixture.get(rec.candidate.fixture.fixture_id))
        for i, rec in enumerate(recommendations)
    ]
    return [header] + cards + [DISCLAIMER]
