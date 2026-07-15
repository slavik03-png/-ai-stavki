"""
Unit tests for ai_predictions/live_report.py -- Live card rendering
(Task #11). Confirms no internal/technical jargon (rationale, edge, EV,
bookmaker counts) leaks onto the user-facing card, and that the three
empty-result cases produce distinct, honest messages.
"""

import datetime
import sys

sys.path.insert(0, ".")

from ai_predictions.live_candidates import LiveCandidate
from ai_predictions.live_fixtures import LiveFixture
from ai_predictions.live_report import render_live_message
from ai_predictions.value_engine import ValueCandidate

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def _live_candidate():
    lf = LiveFixture(
        fixture_id=1, kickoff_utc=datetime.datetime(2026, 7, 15, 11, 0, tzinfo=datetime.timezone.utc),
        home_team="Home FC", away_team="Away FC", league_name="Test League",
        league_country="Testland", status_short="2H", elapsed_minutes=67, home_score=1, away_score=0,
    )
    vc = ValueCandidate(
        event_id="evt-1", sport="football", league="Test League", country="Testland",
        match_datetime="2026-07-15T11:00:00+00:00", home_team="Home FC", away_team="Away FC",
        market_type="h2h", selection="Home FC", line=None,
        best_bookmaker="BookD", best_price=2.30, best_price_implied_probability=0.4,
        consensus_probability=0.55, consensus_bookmaker_count=3, fair_price=1.82,
        edge=0.15, expected_value=0.20, bookmaker_count=4,
        signal_level="HIGH", ranking_score=0.9,
        estimated_probability=0.55, rejection_reasons=["internal only"],
    )
    return LiveCandidate(live_fixture=lf, value_candidate=vc)


def test_no_live_matches_message_is_distinct():
    messages = render_live_message([], live_fixture_count=0, matched_fixture_count=0)
    check("no-live-matches case returns one honest message", len(messages) == 1)
    check("message says no matches are live", "нет матчей" in messages[0].lower())


def test_no_matched_odds_message_mentions_the_live_count():
    messages = render_live_message([], live_fixture_count=5, matched_fixture_count=0)
    check("no-odds-matched case is distinct from no-live-matches case", "5" in messages[0])


def test_no_signal_message_mentions_matched_count():
    messages = render_live_message([], live_fixture_count=5, matched_fixture_count=3)
    check("no-signal case mentions the matched count", "3" in messages[0])


def test_card_never_leaks_internal_rationale_field():
    lc = _live_candidate()
    messages = render_live_message([lc], live_fixture_count=1, matched_fixture_count=1)
    joined = "\n".join(messages)
    check("card never contains the raw internal rationale string", "internal only" not in joined)
    check("card never mentions 'edge' or 'EV' jargon", "edge" not in joined.lower() and " ev " not in joined.lower())
    check("card shows the live minute", "67" in joined)
    check("card shows the current score", "1:0" in joined)
    check("card shows the real price and bookmaker", "2.3" in joined and "BookD" in joined)


def run():
    test_no_live_matches_message_is_distinct()
    test_no_matched_odds_message_mentions_the_live_count()
    test_no_signal_message_mentions_matched_count()
    test_card_never_leaks_internal_rationale_field()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
