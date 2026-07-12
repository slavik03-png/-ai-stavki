"""
Deterministic settlement engine.

Given a `Prediction` and the real `EventResult`, decides the prediction's
final status and a short Russian explanation. Never guesses missing data:
whenever the specific numbers a market needs were not retrieved, the result
is `STATUS_UNRESOLVED` with an explanation of exactly what is missing.

Supported markets (`market_type` values):
    1x2, double_chance, draw_no_bet, btts, total_goals, asian_total,
    team_total, first_half_total, second_half_total, goal_both_halves,
    correct_score, corners_total, cards_total, fouls_total, shots_total

`asian_total`, `total_goals`, `team_total`, `first_half_total`,
`second_half_total`, `corners_total`, `cards_total`, `fouls_total` and
`shots_total` all go through the same quarter-line-aware over/under
evaluator, since they share identical win/push/half-win mechanics.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

from tracking.models import (
    EventResult,
    Prediction,
    STATUS_CANCELLED,
    STATUS_HALF_LOST,
    STATUS_HALF_WON,
    STATUS_LOST,
    STATUS_POSTPONED,
    STATUS_RETURNED,
    STATUS_UNRESOLVED,
    STATUS_WON,
)

MARKET_1X2 = "1x2"
MARKET_DOUBLE_CHANCE = "double_chance"
MARKET_DRAW_NO_BET = "draw_no_bet"
MARKET_BTTS = "btts"
MARKET_TOTAL_GOALS = "total_goals"
MARKET_ASIAN_TOTAL = "asian_total"
MARKET_TEAM_TOTAL = "team_total"
MARKET_FIRST_HALF_TOTAL = "first_half_total"
MARKET_SECOND_HALF_TOTAL = "second_half_total"
MARKET_GOAL_BOTH_HALVES = "goal_both_halves"
MARKET_CORRECT_SCORE = "correct_score"
MARKET_CORNERS_TOTAL = "corners_total"
MARKET_CARDS_TOTAL = "cards_total"
MARKET_FOULS_TOTAL = "fouls_total"
MARKET_SHOTS_TOTAL = "shots_total"
MARKET_SPREAD = "spread"

_LINE_MARKETS = {
    MARKET_TOTAL_GOALS, MARKET_ASIAN_TOTAL, MARKET_TEAM_TOTAL,
    MARKET_FIRST_HALF_TOTAL, MARKET_SECOND_HALF_TOTAL,
    MARKET_CORNERS_TOTAL, MARKET_CARDS_TOTAL, MARKET_FOULS_TOTAL,
    MARKET_SHOTS_TOTAL,
}

SettlementOutcome = Tuple[str, str]  # (status, explanation)


# ---------------------------------------------------------------------------
# Quarter-line-aware over/under evaluator
# ---------------------------------------------------------------------------

def _evaluate_single_line(actual: float, line: float, selection: str) -> str:
    if selection == "over":
        if actual > line:
            return STATUS_WON
        if actual < line:
            return STATUS_LOST
        return STATUS_RETURNED
    if selection == "under":
        if actual < line:
            return STATUS_WON
        if actual > line:
            return STATUS_LOST
        return STATUS_RETURNED
    raise ValueError(f"unsupported over/under selection {selection!r}")


def _combine_split(r1: str, r2: str) -> str:
    pair = {r1, r2}
    if pair == {STATUS_WON}:
        return STATUS_WON
    if pair == {STATUS_LOST}:
        return STATUS_LOST
    if pair == {STATUS_RETURNED}:
        return STATUS_RETURNED
    if pair == {STATUS_WON, STATUS_RETURNED}:
        return STATUS_HALF_WON
    if pair == {STATUS_LOST, STATUS_RETURNED}:
        return STATUS_HALF_LOST
    if pair == {STATUS_WON, STATUS_LOST}:
        # Average of a full win and a full loss nets to zero, same as a push.
        return STATUS_RETURNED
    return STATUS_UNRESOLVED


def evaluate_over_under(actual: float, line: float, selection: str) -> str:
    """Evaluates an over/under bet against `line`, supporting quarter lines
    (e.g. 2.25, 2.75) by splitting them into two adjacent half/whole lines
    and combining the two outcomes, exactly as sportsbooks settle them."""
    frac = round(line - math.floor(line), 2)
    if frac in (0.25, 0.75):
        lower = line - 0.25
        upper = line + 0.25
        r1 = _evaluate_single_line(actual, lower, selection)
        r2 = _evaluate_single_line(actual, upper, selection)
        return _combine_split(r1, r2)
    return _evaluate_single_line(actual, line, selection)


def _unresolved(reason: str) -> SettlementOutcome:
    return STATUS_UNRESOLVED, reason


# ---------------------------------------------------------------------------
# Per-market settlement
# ---------------------------------------------------------------------------

def _settle_1x2(p: Prediction, r: EventResult) -> SettlementOutcome:
    if r.home_goals is None or r.away_goals is None:
        return _unresolved("итоговый счёт матча недоступен")
    if r.home_goals > r.away_goals:
        actual = "home"
    elif r.home_goals < r.away_goals:
        actual = "away"
    else:
        actual = "draw"
    if p.selection == actual:
        return STATUS_WON, f"исход {actual} подтверждён счётом {r.final_score}"
    return STATUS_LOST, f"фактический исход {actual}, счёт {r.final_score}"


def _settle_double_chance(p: Prediction, r: EventResult) -> SettlementOutcome:
    if r.home_goals is None or r.away_goals is None:
        return _unresolved("итоговый счёт матча недоступен")
    if r.home_goals > r.away_goals:
        actual = "home"
    elif r.home_goals < r.away_goals:
        actual = "away"
    else:
        actual = "draw"
    pairs = {"1x": {"home", "draw"}, "x2": {"draw", "away"}, "12": {"home", "away"}}
    covered = pairs.get(p.selection)
    if covered is None:
        raise ValueError(f"unsupported double_chance selection {p.selection!r}")
    if actual in covered:
        return STATUS_WON, f"исход {actual} покрыт двойным шансом {p.selection}"
    return STATUS_LOST, f"фактический исход {actual} не покрыт двойным шансом {p.selection}"


def _settle_draw_no_bet(p: Prediction, r: EventResult) -> SettlementOutcome:
    if r.home_goals is None or r.away_goals is None:
        return _unresolved("итоговый счёт матча недоступен")
    if r.home_goals == r.away_goals:
        return STATUS_RETURNED, "ничья — ставка возвращена (ставка без ничьей)"
    winner = "home" if r.home_goals > r.away_goals else "away"
    if p.selection == winner:
        return STATUS_WON, f"победа {winner} подтверждена счётом {r.final_score}"
    return STATUS_LOST, f"победил {winner}, счёт {r.final_score}"


def _settle_btts(p: Prediction, r: EventResult) -> SettlementOutcome:
    if r.home_goals is None or r.away_goals is None:
        return _unresolved("итоговый счёт матча недоступен")
    both_scored = r.home_goals > 0 and r.away_goals > 0
    actual = "yes" if both_scored else "no"
    if p.selection == actual:
        return STATUS_WON, f"обе забьют: {actual}, счёт {r.final_score}"
    return STATUS_LOST, f"фактически обе забьют: {actual}, счёт {r.final_score}"


def _settle_goal_both_halves(p: Prediction, r: EventResult) -> SettlementOutcome:
    if r.ht_home_goals is None or r.ht_away_goals is None or r.home_goals is None or r.away_goals is None:
        return _unresolved("нет данных по таймам (счёт на перерыве недоступен)")
    first_half_total = r.ht_home_goals + r.ht_away_goals
    second_half_total = (r.home_goals + r.away_goals) - first_half_total
    actual = "yes" if (first_half_total > 0 and second_half_total > 0) else "no"
    if p.selection == actual:
        return STATUS_WON, "гол в обоих таймах подтверждён" if actual == "yes" else "гола не было хотя бы в одном тайме, как и прогнозировалось"
    return STATUS_LOST, f"фактически: {'гол был в обоих таймах' if actual == 'yes' else 'гола не было хотя бы в одном тайме'}"


def _settle_correct_score(p: Prediction, r: EventResult) -> SettlementOutcome:
    if r.home_goals is None or r.away_goals is None:
        return _unresolved("итоговый счёт матча недоступен")
    actual = r.final_score
    if p.selection == actual:
        return STATUS_WON, f"точный счёт {actual} подтверждён"
    return STATUS_LOST, f"фактический счёт {actual}, прогноз был {p.selection}"


def _line_settlement(actual: Optional[float], line: Optional[float], selection: str,
                      missing_reason: str) -> SettlementOutcome:
    if actual is None or line is None:
        return _unresolved(missing_reason)
    status = evaluate_over_under(actual, line, selection)
    if status == STATUS_UNRESOLVED:
        return _unresolved("не удалось однозначно рассчитать результат по линии")
    return status, f"фактическое значение {actual}, линия {line} ({selection})"


def _settle_total_goals(p: Prediction, r: EventResult) -> SettlementOutcome:
    actual = None if r.home_goals is None or r.away_goals is None else r.home_goals + r.away_goals
    return _line_settlement(actual, p.line, p.selection, "итоговый счёт матча недоступен")


def _settle_team_total(p: Prediction, r: EventResult) -> SettlementOutcome:
    # selection format: "home_over" | "home_under" | "away_over" | "away_under"
    try:
        side, direction = p.selection.split("_", 1)
    except ValueError:
        raise ValueError(f"unsupported team_total selection {p.selection!r}")
    if side == "home":
        actual = r.home_goals
    elif side == "away":
        actual = r.away_goals
    else:
        raise ValueError(f"unsupported team_total side {side!r}")
    return _line_settlement(actual, p.line, direction, f"голы команды ({side}) недоступны")


def _settle_first_half_total(p: Prediction, r: EventResult) -> SettlementOutcome:
    actual = None if r.ht_home_goals is None or r.ht_away_goals is None else r.ht_home_goals + r.ht_away_goals
    return _line_settlement(actual, p.line, p.selection, "счёт на перерыве недоступен")


def _settle_second_half_total(p: Prediction, r: EventResult) -> SettlementOutcome:
    if r.ht_home_goals is None or r.ht_away_goals is None or r.home_goals is None or r.away_goals is None:
        return _unresolved("счёт на перерыве или итоговый счёт недоступен")
    actual = (r.home_goals + r.away_goals) - (r.ht_home_goals + r.ht_away_goals)
    return _line_settlement(actual, p.line, p.selection, "счёт на перерыве или итоговый счёт недоступен")


def _settle_corners_total(p: Prediction, r: EventResult) -> SettlementOutcome:
    actual = None if r.home_corners is None or r.away_corners is None else r.home_corners + r.away_corners
    return _line_settlement(actual, p.line, p.selection, "статистика угловых недоступна")


def _settle_cards_total(p: Prediction, r: EventResult) -> SettlementOutcome:
    actual = None if r.home_cards is None or r.away_cards is None else r.home_cards + r.away_cards
    return _line_settlement(actual, p.line, p.selection, "статистика карточек недоступна")


def _settle_fouls_total(p: Prediction, r: EventResult) -> SettlementOutcome:
    actual = None if r.home_fouls is None or r.away_fouls is None else r.home_fouls + r.away_fouls
    return _line_settlement(actual, p.line, p.selection, "статистика фолов недоступна")


def _settle_shots_total(p: Prediction, r: EventResult) -> SettlementOutcome:
    actual = None if r.home_shots is None or r.away_shots is None else r.home_shots + r.away_shots
    return _line_settlement(actual, p.line, p.selection, "статистика ударов недоступна")


def _settle_spread(p: Prediction, r: EventResult) -> SettlementOutcome:
    """Handicap/spread settlement (selection = 'home' | 'away', p.line is the
    handicap exactly as offered for that selection, e.g. -2, +1.5, -0.75).
    Reuses evaluate_over_under's quarter-line push/half-win logic: covering
    a handicap H is equivalent to "goal difference in the selected team's
    favour > -H", so the fractional (.25/.75) split-line mechanics already
    used for totals apply unchanged."""
    if r.home_goals is None or r.away_goals is None:
        return _unresolved("итоговый счёт матча недоступен")
    if p.line is None:
        return _unresolved("гандикап (линия) не указан")
    if p.selection == "home":
        actual = r.home_goals - r.away_goals
    elif p.selection == "away":
        actual = r.away_goals - r.home_goals
    else:
        raise ValueError(f"unsupported spread selection {p.selection!r}")
    status = evaluate_over_under(actual, -p.line, "over")
    if status == STATUS_UNRESOLVED:
        return _unresolved("не удалось однозначно рассчитать результат по гандикапу")
    return status, f"разница голов {actual:+d}, гандикап {p.line:+g} ({p.selection}), счёт {r.final_score}"


_SETTLERS = {
    MARKET_1X2: _settle_1x2,
    MARKET_DOUBLE_CHANCE: _settle_double_chance,
    MARKET_DRAW_NO_BET: _settle_draw_no_bet,
    MARKET_BTTS: _settle_btts,
    MARKET_TOTAL_GOALS: _settle_total_goals,
    MARKET_ASIAN_TOTAL: _settle_total_goals,
    MARKET_TEAM_TOTAL: _settle_team_total,
    MARKET_FIRST_HALF_TOTAL: _settle_first_half_total,
    MARKET_SECOND_HALF_TOTAL: _settle_second_half_total,
    MARKET_GOAL_BOTH_HALVES: _settle_goal_both_halves,
    MARKET_CORRECT_SCORE: _settle_correct_score,
    MARKET_CORNERS_TOTAL: _settle_corners_total,
    MARKET_CARDS_TOTAL: _settle_cards_total,
    MARKET_FOULS_TOTAL: _settle_fouls_total,
    MARKET_SHOTS_TOTAL: _settle_shots_total,
    MARKET_SPREAD: _settle_spread,
}

SUPPORTED_MARKET_TYPES = frozenset(_SETTLERS.keys())


def settle_prediction(prediction: Prediction, result: EventResult) -> SettlementOutcome:
    """Returns (status, explanation) for `prediction` given `result`.

    Never raises for missing data -- returns STATUS_UNRESOLVED with an
    explanation instead. Raises ValueError only for a market_type/selection
    combination the engine does not know how to grade at all (a real
    programming error, not a data-availability issue).
    """
    if result.status == "postponed":
        return STATUS_POSTPONED, "матч перенесён, результат ещё не определён"
    if result.status == "cancelled":
        return STATUS_CANCELLED, "матч отменён"
    if result.status != "finished":
        return _unresolved("матч ещё не завершён или его статус не подтверждён")

    settler = _SETTLERS.get(prediction.market_type)
    if settler is None:
        raise ValueError(f"unsupported market_type {prediction.market_type!r}")
    return settler(prediction, result)
