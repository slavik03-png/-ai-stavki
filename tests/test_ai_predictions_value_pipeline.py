"""
Unit tests for ai_predictions/value_pipeline.py and value_selector.py, with
the network layer (fetch_football_events) monkeypatched -- no real HTTP
calls happen in this test file. Uses only real-shaped (synthetic)
bookmaker JSON, never football statistics.
"""

import datetime
import os
import sys
import tempfile

sys.path.insert(0, ".")

import ai_predictions.value_pipeline as value_pipeline_mod
from tracking.storage import TrackingStorage

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


NOW = datetime.datetime(2026, 7, 12, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _h2h_event(event_id, home, away, prices, hours_ahead=5):
    return {
        "id": event_id,
        "_sport_key": "soccer_epl",
        "sport_title": "Mock League",
        "commence_time": (NOW + datetime.timedelta(hours=hours_ahead)).isoformat(),
        "home_team": home,
        "away_team": away,
        "bookmakers": [
            {
                "title": title,
                "last_update": "2026-07-12T10:00:00Z",
                "markets": [{
                    "key": "h2h",
                    "outcomes": [
                        {"name": home, "price": h},
                        {"name": "Draw", "price": d},
                        {"name": away, "price": a},
                    ],
                }],
            }
            for title, h, d, a in prices
        ],
    }


DIVERGENT_PRICES = [
    ("BookA", 2.00, 3.30, 4.00),
    ("BookB", 1.98, 3.35, 4.05),
    ("BookC", 2.02, 3.25, 3.95),
    ("BookD", 2.30, 3.10, 3.60),  # genuine outlier
]

FLAT_PRICES = [
    ("BookA", 2.00, 3.30, 4.00),
    ("BookB", 2.01, 3.29, 3.99),
    ("BookC", 1.99, 3.31, 4.01),
]


def _fake_fixtures_for_events(events):
    """Builds a real-shaped FixtureDiscoveryResult with one Fixture per
    event, matching name/kickoff exactly -- so fixture_matching.py's
    confidence floor always accepts it -- and lets tests exercise the
    fixture-discovery-first pipeline without a real API-Football call."""
    from ai_predictions.fixtures import Fixture, FixtureDiscoveryResult
    from ai_predictions.window import parse_commence_time

    fixtures = []
    for i, event in enumerate(events, start=1):
        fixtures.append(Fixture(
            fixture_id=1000 + i,
            kickoff_utc=parse_commence_time(event["commence_time"]),
            home_team=event["home_team"],
            away_team=event["away_team"],
            home_team_id=None,
            away_team_id=None,
            league_name="Mock League",
            league_country="England",
            status_short="NS",
        ))
    return FixtureDiscoveryResult(fixtures=fixtures, dates_queried=["2026-07-12"])


def _run_with_events(events, tmp_db_path):
    # Fixture discovery normally comes from API-Football first; tests
    # supply real-shaped synthetic fixtures matching the synthetic odds
    # events instead of hitting the network. The pipeline's own 36h window
    # filter still applies to the ODDS events before fixture matching, so
    # an out-of-window odds event is excluded regardless of the fixture.
    value_pipeline_mod.discover_fixtures_in_window = (
        lambda api_key, cache, now, window_hours=36: _fake_fixtures_for_events(events)
    )
    value_pipeline_mod.fetch_active_sports = lambda api_key=None, persistent_cache=None: (
        [{"key": "soccer_epl", "group": "Soccer", "title": "EPL", "description": "England Premier League",
          "active": True, "has_outrights": False}],
        None,
    )
    value_pipeline_mod.fetch_football_events = lambda api_key=None, sport_keys=None, persistent_cache=None: (
        events, "500", [],
    )
    storage = TrackingStorage(db_path=tmp_db_path)
    # football_api_key="" explicitly disables statistics enrichment (never
    # falls back to a real FOOTBALL_API_KEY from the environment) so these
    # odds-only tests never make a real network call.
    result = value_pipeline_mod.run_value_predictions(
        odds_api_key="fake-odds-key", football_api_key="", storage=storage, now=NOW,
    )
    return result, storage


def test_out_of_window_event_excluded():
    with tempfile.TemporaryDirectory() as d:
        event = _h2h_event("evt-far", "Home FC", "Away FC", DIVERGENT_PRICES, hours_ahead=48)
        result, storage = _run_with_events([event], os.path.join(d, "t.db"))
        check("far-future event does not produce a recommendation", result.final_recommendations == 0)
        check("excluded-by-window count reflects the one event", result.events_excluded_by_window == 1)
        storage.close()


def test_genuine_divergence_produces_saved_recommendation():
    with tempfile.TemporaryDirectory() as d:
        event = _h2h_event("evt-divergent", "Home FC", "Away FC", DIVERGENT_PRICES)
        result, storage = _run_with_events([event], os.path.join(d, "t.db"))
        # DIVERGENT_PRICES' best price is >10% above the real second-best
        # price -- a genuine isolated-outlier shape -- so it is correctly
        # demoted from HIGH to MEDIUM rather than rejected outright.
        check("one recommendation produced from genuine divergence", result.final_recommendations == 1, result.final_recommendations)
        check("MEDIUM count reflects the outlier-demoted signal", result.medium_count == 1, result.medium_count)
        check("diagnostics report events received", result.events_received == 1)
        check("diagnostics report candidates created", result.candidates_created > 0)
        check("report text names the divergence, not fabricated stats", "расхожд" in result.report_text.lower())

        # Every evaluated candidate (including REJECTED ones for Draw/Away)
        # is now persisted, per spec Step 7 -- not just the shown signal.
        saved = storage.list_all_predictions()
        check("every evaluated candidate this run was persisted, not just the shown one",
              result.saved_count == len(saved) and len(saved) >= 1, (result.saved_count, len(saved)))
        shown = [r for r in saved if r["signal_level"] == "MEDIUM"]
        check("the shown MEDIUM candidate is among the persisted rows", len(shown) == 1, shown)
        check("saved prediction is tagged with a real data provider", shown[0]["data_provider"] in ("the_odds_api", "the_odds_api+api_football"), shown[0]["data_provider"])
        check("saved prediction is tagged with the current model version", shown[0]["model_version"] == "value-ranking-v2.1", shown[0]["model_version"])
        storage.close()


def test_flat_market_yields_no_recommendations_and_explains_why():
    with tempfile.TemporaryDirectory() as d:
        event = _h2h_event("evt-flat", "Home FC", "Away FC", FLAT_PRICES)
        result, storage = _run_with_events([event], os.path.join(d, "t.db"))
        check("flat/agreeing market yields zero recommendations", result.final_recommendations == 0)
        check("REJECTED candidates are still saved even though nothing is shown",
              result.saved_count > 0 and result.candidates_rejected == result.saved_count, result.saved_count)
        check("report explicitly says no signals at any level, never invents one",
              "нет сигналов" in result.report_text.lower())
        storage.close()


def test_at_most_one_recommendation_per_event():
    with tempfile.TemporaryDirectory() as d:
        # Same event, but with a totals market that also diverges strongly --
        # only one recommendation (the strongest) should survive per event.
        event = _h2h_event("evt-multi", "Home FC", "Away FC", DIVERGENT_PRICES)
        event["bookmakers"].append({
            "title": "BookE",
            "last_update": "2026-07-12T10:00:00Z",
            "markets": [{
                "key": "totals",
                "outcomes": [
                    {"name": "Over", "price": 3.50, "point": 2.5},
                    {"name": "Under", "price": 1.30, "point": 2.5},
                ],
            }],
        })
        for title, over_p, under_p in [("BookF", 1.90, 1.90), ("BookG", 1.92, 1.88), ("BookH", 1.88, 1.92)]:
            event["bookmakers"].append({
                "title": title,
                "last_update": "2026-07-12T10:00:00Z",
                "markets": [{
                    "key": "totals",
                    "outcomes": [
                        {"name": "Over", "price": over_p, "point": 2.5},
                        {"name": "Under", "price": under_p, "point": 2.5},
                    ],
                }],
            })
        result, storage = _run_with_events([event], os.path.join(d, "t.db"))
        # Two different real markets (1x2 and totals) both diverging is the
        # documented exception (Step 6): up to 2 signals survive for one
        # event ONLY if both are MEDIUM or HIGH -- never more than that.
        check("at most two recommendations survive for a single event (different-market exception)",
              result.final_recommendations <= 2, result.final_recommendations)
        storage.close()


def test_max_five_recommendations_across_events():
    with tempfile.TemporaryDirectory() as d:
        events = [
            _h2h_event(f"evt-{i}", f"Home{i} FC", f"Away{i} FC", DIVERGENT_PRICES)
            for i in range(8)
        ]
        result, storage = _run_with_events(events, os.path.join(d, "t.db"))
        check("never more than 5 main recommendations", result.final_recommendations <= 5, result.final_recommendations)
        storage.close()


def test_no_events_yields_honest_empty_report():
    with tempfile.TemporaryDirectory() as d:
        result, storage = _run_with_events([], os.path.join(d, "t.db"))
        check("zero events -> zero saved", result.saved_count == 0)
        check("report explicitly explains the empty result",
              "нет сигналов" in result.report_text.lower(),
              result.report_text[:300])
        storage.close()


def run():
    test_out_of_window_event_excluded()
    test_genuine_divergence_produces_saved_recommendation()
    test_flat_market_yields_no_recommendations_and_explains_why()
    test_at_most_one_recommendation_per_event()
    test_max_five_recommendations_across_events()
    test_no_events_yields_honest_empty_report()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
