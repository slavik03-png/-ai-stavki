"""
Telegram rendering for the API-Football-only production v3 pipeline
(ai_predictions/football_pipeline.py). Deliberately separate from
ai_predictions/value_report.py (the older odds-driven report) -- the
card format, heading and no-signal text below are exact, spec-mandated
strings, not derived from the older report's layout.

Nothing here shows a bookmaker name, an edge/EV number, an internal
scoring formula, an API identifier, a raw rejection list, an HTTP error
or any other technical detail -- see tests/test_football_pipeline_cards.py.
"""

from __future__ import annotations

from typing import List, Optional

from ai_predictions.prediction_selector import RankedRecommendation
from ai_predictions.ru_translation import country_ru, league_ru
from ai_predictions.value_config import SIGNAL_LABELS_RU_CARD
from ai_predictions.window import format_display_time

NO_SIGNAL_TEMPLATE = (
    "На ближайшие 36 часов найдено {count} матчей, но ни один вариант не достиг "
    "минимальной расчётной вероятности 56%. Слабые ставки бот не предлагает."
)

HEADING = "🤖 ПРОГНОЗЫ ИИ НА БЛИЖАЙШИЕ 36 ЧАСОВ"


def render_no_signal_message(found_fixtures: int) -> str:
    return NO_SIGNAL_TEMPLATE.format(count=found_fixtures)


def _format_probability(probability: float) -> str:
    return f"{round(probability * 100)}%"


def _format_odds(odds: Optional[float]) -> str:
    if odds is None:
        return "Коэффициент: нет данных"
    return f"Коэффициент: {odds:.2f}"


def render_recommendation_card(index: int, recommendation: RankedRecommendation, odds: Optional[float]) -> str:
    c = recommendation.candidate
    fixture = c.fixture
    tier_label = SIGNAL_LABELS_RU_CARD[recommendation.signal_level]
    country = country_ru(fixture.league_country) or "неизвестно"
    league = league_ru(fixture.league_name) or "неизвестно"
    when = format_display_time(fixture.kickoff_utc)

    lines = [
        f"{index}. {tier_label}",
        "",
        f"Страна: {country}",
        f"Турнир: {league}",
        f"Матч: {fixture.home_team} — {fixture.away_team}",
        f"Дата и время: {when}",
        f"Ставка: {c.market_label_ru}",
        f"Расчётная вероятность: {_format_probability(c.probability)}",
        _format_odds(odds),
        "",
        f"Краткое обоснование: {c.rationale}",
    ]
    return "\n".join(lines)


def render_predictions_message(
    recommendations: List[RankedRecommendation],
    odds_by_fixture: dict,
    *,
    found_fixtures: int,
    analysed_fixtures: int,
) -> List[str]:
    """Returns one or more Telegram message chunks -- the heading (with
    Found/Analysed/Recommendations counts) followed by every card, or the
    exact no-signal message if `recommendations` is empty."""
    if not recommendations:
        return [render_no_signal_message(found_fixtures)]

    header = (
        f"{HEADING}\n\n"
        f"Найдено матчей: {found_fixtures}\n"
        f"Проанализировано: {analysed_fixtures}\n"
        f"Рекомендаций: {len(recommendations)}"
    )
    cards = [
        render_recommendation_card(i + 1, rec, odds_by_fixture.get(rec.candidate.fixture.fixture_id))
        for i, rec in enumerate(recommendations)
    ]
    return [header] + cards
