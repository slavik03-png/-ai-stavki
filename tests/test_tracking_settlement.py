"""
Settlement-rule tests for tracking/settlement.py.

Covers every supported market_type, quarter-line Asian totals, half-win /
half-loss math, postponed/cancelled events, and the "missing data means
unresolved, never a guess" rule.
"""

import sys

sys.path.insert(0, ".")

from tracking.models import (
    Prediction, EventResult,
    STATUS_WON, STATUS_LOST, STATUS_RETURNED, STATUS_HALF_WON, STATUS_HALF_LOST,
    STATUS_POSTPONED, STATUS_CANCELLED, STATUS_UNRESOLVED,
)
from tracking.settlement import settle_prediction, evaluate_over_under

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def _p(market_type, selection, line=None, **overrides):
    base = dict(
        sport="football", country="England", league="Premier League",
        event_id="evt-1", event_start_time="2026-07-10T18:00:00+00:00",
        home_team="Arsenal", away_team="Chelsea",
        market_type=market_type, market_name="test", selection=selection, line=line,
        bookmaker_odds=1.9, model_probability=0.55, confidence_score=60.0,
        confidence_level="средняя уверенность", recommendation_group="main",
        explanation="test", data_provider="mock", model_version="v1",
    )
    base.update(overrides)
    return Prediction(**base)


def _finished(**kwargs) -> EventResult:
    return EventResult(event_id="evt-1", status="finished", **kwargs)


def test_1x2():
    r = _finished(home_goals=2, away_goals=1)
    status, _ = settle_prediction(_p("1x2", "home"), r)
    check("1x2 home win settles won", status == STATUS_WON)
    status, _ = settle_prediction(_p("1x2", "away"), r)
    check("1x2 wrong selection settles lost", status == STATUS_LOST)
    status, _ = settle_prediction(_p("1x2", "draw"), _finished(home_goals=1, away_goals=1))
    check("1x2 draw settles won when selection is draw", status == STATUS_WON)


def test_double_chance():
    r = _finished(home_goals=1, away_goals=1)
    status, _ = settle_prediction(_p("double_chance", "1x"), r)
    check("double chance 1X wins on a draw", status == STATUS_WON)
    status, _ = settle_prediction(_p("double_chance", "12"), r)
    check("double chance 12 loses on a draw", status == STATUS_LOST)


def test_draw_no_bet():
    draw = _finished(home_goals=0, away_goals=0)
    status, _ = settle_prediction(_p("draw_no_bet", "home"), draw)
    check("draw no bet returns stake on a draw", status == STATUS_RETURNED)
    win = _finished(home_goals=2, away_goals=0)
    status, _ = settle_prediction(_p("draw_no_bet", "home"), win)
    check("draw no bet wins on a home win", status == STATUS_WON)
    status, _ = settle_prediction(_p("draw_no_bet", "away"), win)
    check("draw no bet loses on the other side", status == STATUS_LOST)


def test_btts():
    both = _finished(home_goals=2, away_goals=1)
    status, _ = settle_prediction(_p("btts", "yes"), both)
    check("btts yes settles won when both score", status == STATUS_WON)
    one = _finished(home_goals=2, away_goals=0)
    status, _ = settle_prediction(_p("btts", "no"), one)
    check("btts no settles won when only one team scores", status == STATUS_WON)


def test_total_goals_whole_line_push():
    r = _finished(home_goals=1, away_goals=2)  # total = 3
    status, _ = settle_prediction(_p("total_goals", "over", line=3.0), r)
    check("total goals over on exact whole line pushes (returned)", status == STATUS_RETURNED)
    status, _ = settle_prediction(_p("total_goals", "over", line=2.5), r)
    check("total goals over half line with total 3 wins", status == STATUS_WON)
    status, _ = settle_prediction(_p("total_goals", "under", line=2.5), r)
    check("total goals under half line with total 3 loses", status == STATUS_LOST)


def test_quarter_line_asian_total_half_win():
    # total = 3 goals, quarter line 2.75 -> splits into 2.5 (win) and 3.0 (push) -> half_won
    r = _finished(home_goals=1, away_goals=2)
    status, expl = settle_prediction(_p("asian_total", "over", line=2.75), r)
    check("quarter line 2.75 over with total=3 is half_won", status == STATUS_HALF_WON, expl)


def test_quarter_line_asian_total_half_lost():
    # total = 2 goals, quarter line 2.25 over -> splits into 2.0 (push) and 2.5 (lost) -> half_lost
    r = _finished(home_goals=1, away_goals=1)
    status, expl = settle_prediction(_p("asian_total", "over", line=2.25), r)
    check("quarter line 2.25 over with total=2 is half_lost", status == STATUS_HALF_LOST, expl)


def test_evaluate_over_under_helper_matches_settlement():
    check("evaluate_over_under exposed and consistent",
          evaluate_over_under(3, 2.75, "over") == STATUS_HALF_WON)


def test_team_total():
    r = _finished(home_goals=2, away_goals=0)
    status, _ = settle_prediction(_p("team_total", "home_over", line=1.5), r)
    check("team_total home_over wins when home scored more than the line", status == STATUS_WON)
    status, _ = settle_prediction(_p("team_total", "away_over", line=0.5), r)
    check("team_total away_over loses when away team didn't score", status == STATUS_LOST)


def test_first_and_second_half_totals():
    r = _finished(home_goals=2, away_goals=1, ht_home_goals=1, ht_away_goals=0)
    status, _ = settle_prediction(_p("first_half_total", "over", line=0.5), r)
    check("first half total over 0.5 wins (1 goal in first half)", status == STATUS_WON)
    status, _ = settle_prediction(_p("second_half_total", "over", line=1.5), r)
    # second half goals = (2+1) - (1+0) = 2
    check("second half total over 1.5 wins (2 goals in second half)", status == STATUS_WON)


def test_goal_in_both_halves():
    r = _finished(home_goals=2, away_goals=1, ht_home_goals=1, ht_away_goals=0)
    status, _ = settle_prediction(_p("goal_both_halves", "yes"), r)
    check("goal in both halves settles won when both halves had a goal", status == STATUS_WON)
    r2 = _finished(home_goals=2, away_goals=0, ht_home_goals=2, ht_away_goals=0)
    status, _ = settle_prediction(_p("goal_both_halves", "no"), r2)
    check("goal in both halves settles won (no) when second half was scoreless", status == STATUS_WON)


def test_correct_score():
    r = _finished(home_goals=2, away_goals=1)
    status, _ = settle_prediction(_p("correct_score", "2:1"), r)
    check("correct score matches exactly settles won", status == STATUS_WON)
    status, _ = settle_prediction(_p("correct_score", "1:1"), r)
    check("correct score mismatch settles lost", status == STATUS_LOST)


def test_corners_cards_fouls_shots_totals():
    r = _finished(home_goals=1, away_goals=1, home_corners=6, away_corners=4,
                  home_cards=2, away_cards=3, home_fouls=10, away_fouls=8,
                  home_shots=12, away_shots=9)
    status, _ = settle_prediction(_p("corners_total", "over", line=8.5), r)
    check("corners total over settles correctly", status == STATUS_WON)  # 10 > 8.5
    status, _ = settle_prediction(_p("cards_total", "under", line=4.5), r)
    check("cards total under settles correctly", status == STATUS_LOST)  # 5 > 4.5
    status, _ = settle_prediction(_p("fouls_total", "over", line=15.5), r)
    check("fouls total over settles correctly", status == STATUS_WON)  # 18 > 15.5
    status, _ = settle_prediction(_p("shots_total", "under", line=25.5), r)
    check("shots total under settles correctly", status == STATUS_WON)  # 21 < 25.5


def test_postponed_and_cancelled():
    postponed = EventResult(event_id="evt-1", status="postponed")
    status, _ = settle_prediction(_p("1x2", "home"), postponed)
    check("postponed match settles as postponed", status == STATUS_POSTPONED)

    cancelled = EventResult(event_id="evt-1", status="cancelled")
    status, _ = settle_prediction(_p("1x2", "home"), cancelled)
    check("cancelled match settles as cancelled", status == STATUS_CANCELLED)


def test_missing_data_never_guessed():
    incomplete = EventResult(event_id="evt-1", status="finished")  # no goals at all
    status, explanation = settle_prediction(_p("1x2", "home"), incomplete)
    check("missing goals produce unresolved, not a guess", status == STATUS_UNRESOLVED)
    check("unresolved status carries an explanation", bool(explanation))

    no_corners = _finished(home_goals=1, away_goals=1)  # corners not retrieved
    status, explanation = settle_prediction(_p("corners_total", "over", line=8.5), no_corners)
    check("missing corners data produces unresolved, not a guess", status == STATUS_UNRESOLVED)

    no_half_data = _finished(home_goals=1, away_goals=1)  # no ht_* fields
    status, _ = settle_prediction(_p("first_half_total", "over", line=0.5), no_half_data)
    check("missing half-time data produces unresolved for first-half markets", status == STATUS_UNRESOLVED)

    unfinished = EventResult(event_id="evt-1", status="unknown")
    status, _ = settle_prediction(_p("1x2", "home"), unfinished)
    check("unconfirmed match status produces unresolved", status == STATUS_UNRESOLVED)


def test_spread():
    r = _finished(home_goals=2, away_goals=0)
    status, _ = settle_prediction(_p("spread", "home", line=-1.5), r)
    check("home -1.5 covers when home wins by 2", status == STATUS_WON, status)
    status, _ = settle_prediction(_p("spread", "home", line=-2.5), r)
    check("home -2.5 does not cover when home wins by only 2", status == STATUS_LOST, status)
    status, _ = settle_prediction(_p("spread", "home", line=-2.0), r)
    check("home -2.0 pushes exactly on a 2-goal win", status == STATUS_RETURNED, status)
    status, _ = settle_prediction(_p("spread", "away", line=1.5), r)
    check("away +1.5 loses when away loses by 2", status == STATUS_LOST, status)
    status, _ = settle_prediction(_p("spread", "home", line=-2.25), r)
    check("home -2.25 quarter line half-loses on an exact 2-goal win", status == STATUS_HALF_LOST, status)
    status, _ = settle_prediction(_p("spread", "home", line=None), r)
    check("missing handicap line produces unresolved, never guessed", status == STATUS_UNRESOLVED, status)


def run():
    test_1x2()
    test_double_chance()
    test_draw_no_bet()
    test_btts()
    test_total_goals_whole_line_push()
    test_quarter_line_asian_total_half_win()
    test_quarter_line_asian_total_half_lost()
    test_evaluate_over_under_helper_matches_settlement()
    test_team_total()
    test_first_and_second_half_totals()
    test_goal_in_both_halves()
    test_correct_score()
    test_corners_cards_fouls_shots_totals()
    test_postponed_and_cancelled()
    test_missing_data_never_guessed()
    test_spread()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
