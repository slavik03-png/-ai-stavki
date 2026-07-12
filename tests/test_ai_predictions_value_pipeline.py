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


def _run_with_events(events, tmp_db_path):
    value_pipeline_mod.fetch_football_events = lambda api_key=None: (events, "500", [])
    storage = TrackingStorage(db_path=tmp_db_path)
    result = value_pipeline_mod.run_value_predictions(odds_api_key="fake-odds-key", storage=storage, now=NOW)
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
        check("one recommendation produced from genuine divergence", result.final_recommendations == 1, result.final_recommendations)
        check("recommendation was saved to tracking storage", result.saved_count == 1, result.saved_count)
        check("diagnostics report events received", result.events_received == 1)
        check("diagnostics report candidates created", result.candidates_created > 0)
        check("report text names the divergence, not fabricated stats", "расхожд" in result.report_text.lower())

        saved = storage.list_all_predictions()
        check("exactly one prediction persisted", len(saved) == 1)
        check("saved prediction is tagged odds-only data provider", saved[0]["data_provider"] == "the_odds_api", saved[0]["data_provider"])
        storage.close()


def test_flat_market_yields_no_recommendations_and_explains_why():
    with tempfile.TemporaryDirectory() as d:
        event = _h2h_event("evt-flat", "Home FC", "Away FC", FLAT_PRICES)
        result, storage = _run_with_events([event], os.path.join(d, "t.db"))
        check("flat/agreeing market yields zero recommendations", result.final_recommendations == 0)
        check("nothing saved when nothing qualifies", result.saved_count == 0)
        check("report explicitly says no recommendations, never invents one",
              "нет надёжных рекомендаций" in result.report_text.lower())
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
        check("at most one recommendation survives for a single event", result.final_recommendations <= 1, result.final_recommendations)
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
              "не найдено" in result.report_text.lower() or "нет надёжных рекомендаций" in result.report_text.lower(),
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
