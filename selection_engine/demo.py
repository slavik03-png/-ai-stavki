"""
Standalone demo: builds a handful of mock CandidatePrediction objects and
runs them through the full selection pipeline, printing the resulting
Russian-language report.

This uses only invented/mock numbers for demonstration -- it is not wired
to bot.py, Telegram, or any real odds/statistics API. Run directly:

    python3 -m selection_engine.demo
"""

from __future__ import annotations

import datetime

from selection_engine.config import SelectionConfig
from selection_engine.models import CandidatePrediction
from selection_engine.report import render_daily_report
from selection_engine.selector import select_recommendations


def _future_iso(hours: int) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now + datetime.timedelta(hours=hours)).isoformat()


def build_mock_candidates():
    return [
        CandidatePrediction(
            event_id="mock-1", sport="football", league="Premier League", country="England",
            match_datetime=_future_iso(6), home_team="Arsenal", away_team="Everton",
            market_type="1x2", selection="1", line=None, bookmaker="MockBook",
            odds=1.75, model_probability=0.85, sample_size=32,
            available_fields={
                "home_form": True, "away_form": True, "sample_size": True,
                "h2h": True, "league_position": True, "injuries": True, "lineups": True,
            },
        ),
        CandidatePrediction(
            event_id="mock-2", sport="football", league="La Liga", country="Spain",
            match_datetime=_future_iso(8), home_team="Villarreal", away_team="Getafe",
            market_type="corners_total", selection="over_9.5", line=9.5, bookmaker="MockBook",
            odds=1.75, model_probability=0.85, sample_size=28,
            available_fields={"corners": True, "sample_size": True, "current_price": True},
        ),
        CandidatePrediction(
            event_id="mock-3", sport="football", league="Serie A", country="Italy",
            match_datetime=_future_iso(10), home_team="Bologna", away_team="Torino",
            market_type="btts", selection="yes", line=None, bookmaker="MockBook",
            odds=1.90, model_probability=0.61, sample_size=18,
            available_fields={
                "btts_frequency_home": True, "btts_frequency_away": True, "sample_size": True,
                "clean_sheets_home": True, "clean_sheets_away": True, "goals_scored_conceded": True,
            },
        ),
        CandidatePrediction(
            event_id="mock-4", sport="football", league="Bundesliga", country="Germany",
            match_datetime=_future_iso(7), home_team="Union Berlin", away_team="Mainz",
            market_type="correct_score", selection="1:1", line=None, bookmaker="MockBook",
            odds=7.50, model_probability=0.16, sample_size=15,
            available_fields={
                "goals_scored_conceded": True, "recent_matches": True, "sample_size": True,
                "h2h": True, "lineups": True,
            },
        ),
        CandidatePrediction(
            event_id="mock-5", sport="football", league="Ligue 1", country="France",
            match_datetime=_future_iso(9), home_team="Lens", away_team="Reims",
            market_type="1x2", selection="1", line=None, bookmaker="MockBook",
            odds=1.15, model_probability=0.80, sample_size=20,
            available_fields={
                "home_form": True, "away_form": True, "sample_size": True,
                "h2h": True, "league_position": True, "injuries": True, "lineups": True,
            },
        ),
    ]


def main() -> None:
    config = SelectionConfig()
    candidates = build_mock_candidates()
    result = select_recommendations(candidates, config, storage=None)
    print(render_daily_report(result))


if __name__ == "__main__":
    main()
