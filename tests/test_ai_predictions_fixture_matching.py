"""
Unit tests for ai_predictions/fixture_matching.py: matching real API-Football
fixtures to real The Odds API events by team-name confidence + kickoff
proximity, with ambiguous pairs dropped rather than guessed.
"""

import datetime
import sys

sys.path.insert(0, ".")

from ai_predictions.fixture_matching import match_fixtures_to_events
from ai_predictions.fixtures import Fixture

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


NOW = datetime.datetime(2026, 7, 14, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _fixture(fid, home, away, kickoff):
    return Fixture(
        fixture_id=fid, kickoff_utc=kickoff, home_team=home, away_team=away,
        home_team_id=1, away_team_id=2, league_name="Premier League", league_country="England",
        status_short="NS",
    )


def _event(event_id, home, away, kickoff):
    return {"id": event_id, "home_team": home, "away_team": away, "commence_time": kickoff.isoformat()}


def test_confident_match_on_identical_names_and_close_kickoff():
    kickoff = NOW + datetime.timedelta(hours=5)
    fixture = _fixture(1, "Manchester United", "Chelsea", kickoff)
    event = _event("evt-1", "Manchester United", "Chelsea", kickoff)
    result = match_fixtures_to_events([fixture], [event])
    check("one confident match produced", len(result.matches) == 1, result.matches)
    check("no unmatched fixtures", result.unmatched_fixtures == [])
    check("no unmatched events", result.unmatched_events == [])


def test_kickoff_too_far_apart_is_not_matched():
    fixture = _fixture(2, "Manchester United", "Chelsea", NOW + datetime.timedelta(hours=5))
    event = _event("evt-2", "Manchester United", "Chelsea", NOW + datetime.timedelta(hours=9))
    result = match_fixtures_to_events([fixture], [event])
    check("kickoff drift beyond tolerance blocks the match", result.matches == [])
    check("fixture reported unmatched, not fabricated", len(result.unmatched_fixtures) == 1)


def test_unrelated_team_names_never_matched():
    fixture = _fixture(3, "Manchester United", "Chelsea", NOW + datetime.timedelta(hours=5))
    event = _event("evt-3", "Real Madrid", "Barcelona", NOW + datetime.timedelta(hours=5))
    result = match_fixtures_to_events([fixture], [event])
    check("completely different teams never matched", result.matches == [])
    check("event reported unmatched", len(result.unmatched_events) == 1)


def test_ambiguous_candidates_are_dropped_not_guessed():
    kickoff = NOW + datetime.timedelta(hours=5)
    fixture = _fixture(4, "Manchester United", "Chelsea", kickoff)
    # Two events with near-identical plausibility for the same fixture --
    # neither should be guessed.
    event_a = _event("evt-a", "Manchester United", "Chelsea", kickoff)
    event_b = _event("evt-b", "Manchester United", "Chelsea", kickoff)
    result = match_fixtures_to_events([fixture], [event_a, event_b])
    check("tied candidates are never guessed into a match", result.matches == [])
    check("fixture reported ambiguous, not unmatched", len(result.ambiguous_fixtures) == 1, result.ambiguous_fixtures)


def test_multiple_fixtures_each_get_their_own_event():
    kickoff1 = NOW + datetime.timedelta(hours=5)
    kickoff2 = NOW + datetime.timedelta(hours=10)
    fixture1 = _fixture(5, "Manchester United", "Chelsea", kickoff1)
    fixture2 = _fixture(6, "Arsenal", "Liverpool", kickoff2)
    event1 = _event("evt-5", "Manchester United", "Chelsea", kickoff1)
    event2 = _event("evt-6", "Arsenal", "Liverpool", kickoff2)
    result = match_fixtures_to_events([fixture1, fixture2], [event1, event2])
    check("both independent fixtures matched correctly", len(result.matches) == 2, result.matches)
    matched_pairs = {(m.fixture.fixture_id, m.event["id"]) for m in result.matches}
    check("each fixture paired with its own event, not cross-matched",
          matched_pairs == {(5, "evt-5"), (6, "evt-6")}, matched_pairs)


def run():
    test_confident_match_on_identical_names_and_close_kickoff()
    test_kickoff_too_far_apart_is_not_matched()
    test_unrelated_team_names_never_matched()
    test_ambiguous_candidates_are_dropped_not_guessed()
    test_multiple_fixtures_each_get_their_own_event()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
