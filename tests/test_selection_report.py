"""
Tests for selection_engine/report.py: the daily Russian-language report and
the distinct "no reliable recommendations" report.
"""

import datetime
import sys

sys.path.insert(0, ".")

from selection_engine.config import SelectionConfig
from selection_engine.models import CandidatePrediction
from selection_engine.report import DISCLAIMER, render_daily_report, render_no_recommendation_report
from selection_engine.selector import select_recommendations

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


NOW = datetime.datetime(2026, 7, 12, 12, 0, tzinfo=datetime.timezone.utc)
FUTURE = "2026-07-12T20:00:00+00:00"

FULL_FIELDS_1X2 = {
    "home_form": True, "away_form": True, "sample_size": True,
    "h2h": True, "league_position": True, "injuries": True, "lineups": True,
}


def strong_candidate(event_id, league="Premier League"):
    return CandidatePrediction(
        event_id=event_id, sport="football", league=league, country="England",
        match_datetime=FUTURE, home_team=f"Home-{event_id}", away_team=f"Away-{event_id}",
        market_type="1x2", selection="1", line=None, bookmaker="BookX",
        odds=2.4, model_probability=0.84,
        available_fields=dict(FULL_FIELDS_1X2), sample_size=30,
    )


def test_no_recommendation_report_format():
    result = select_recommendations([], SelectionConfig(), now=NOW)
    text = render_no_recommendation_report(result)
    check("no-recommendation report states there are no reliable recommendations", "нет надёжных рекомендаций" in text.lower())
    check("no-recommendation report lists at least one reason", "Причины:" in text)
    check("no-recommendation report ends with the mandatory disclaimer", DISCLAIMER in text)


def test_daily_report_dispatches_to_no_recommendation_format_when_empty():
    result = select_recommendations(
        [CandidatePrediction(
            event_id="ev1", sport="football", league="X", country="Y",
            match_datetime=FUTURE, home_team="A", away_team="B",
            market_type="1x2", selection="1", line=None, bookmaker="BookX",
            odds=1.05, model_probability=0.55, available_fields={}, sample_size=1,
        )],
        SelectionConfig(), now=NOW,
    )
    text = render_daily_report(result)
    check("render_daily_report falls back to no-recommendation format when MAIN is empty", "нет надёжных рекомендаций" in text.lower())


def test_daily_report_with_main_recommendations():
    candidates = [strong_candidate("ev1", league="League-1"), strong_candidate("ev2", league="League-2")]
    result = select_recommendations(candidates, SelectionConfig(), now=NOW)
    text = render_daily_report(result)
    check("report includes the MAIN section header", "ОСНОВНЫЕ РЕКОМЕНДАЦИИ" in text)
    check("report includes team names for a MAIN recommendation", "Home-ev1" in text)
    check("report includes the odds", "2.40" in text)
    check("report includes the model version", "selection-v1.0" in text)
    check("report ends with the mandatory disclaimer", DISCLAIMER in text)
    check("report does not include the no-recommendation message when MAIN exists", "нет надёжных рекомендаций" not in text.lower())


def test_disclaimer_never_omitted():
    empty_result = select_recommendations([], SelectionConfig(), now=NOW)
    check("disclaimer present even with zero candidates", DISCLAIMER in render_daily_report(empty_result))


def run():
    test_no_recommendation_report_format()
    test_daily_report_dispatches_to_no_recommendation_format_when_empty()
    test_daily_report_with_main_recommendations()
    test_disclaimer_never_omitted()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
