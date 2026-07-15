"""
Cross-bookmaker consensus candidate-building for Live in-play mode
(2026-07-15). Deliberately reuses the exact same real math as
ai_predictions/value_engine.py (leave-one-out consensus vs. best price,
computed purely from real bookmaker rows in ai_predictions/matching.py) --
API-Football's own /predictions endpoint is a PRE-MATCH model with no real
opinion once a match has kicked off, so Live mode is odds-only by design,
not a simplified statistical model.

One real, matched Odds API event -> at most one LiveCandidate per fixture
(its single best real signal, HIGH first then MEDIUM then LOW) -- a
fixture whose matched event has no outcome clearing even LOW is dropped,
never shown with a weak or invented pick.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from ai_predictions.fixture_matching import FixtureMatch
from ai_predictions.matching import (
    RawRow,
    ValidationStats,
    dedupe_bookmaker_rows,
    group_rows,
    raw_bookmaker_row_counts,
    validate_rows,
)
from ai_predictions.value_config import SIGNAL_HIGH, SIGNAL_LOW, SIGNAL_MEDIUM, SIGNAL_REJECTED
from ai_predictions.value_engine import ValueCandidate, build_value_candidates_from_groups

_LEVEL_RANK = {SIGNAL_HIGH: 3, SIGNAL_MEDIUM: 2, SIGNAL_LOW: 1, SIGNAL_REJECTED: 0}


@dataclass
class LiveCandidate:
    live_fixture: object  # ai_predictions.live_fixtures.LiveFixture
    value_candidate: ValueCandidate


def _extract_rows_for_event(match: FixtureMatch) -> List[RawRow]:
    event = match.event
    home_team = event.get("home_team", "")
    away_team = event.get("away_team", "")
    commence_time = event.get("commence_time", "")
    event_id = str(event.get("id") or match.live_fixture.fixture_id)
    league = getattr(match.live_fixture, "league_name", None)

    from ai_predictions.matching import build_event_key
    event_key = build_event_key(event.get("_sport_key"), commence_time, home_team, away_team)

    rows: List[RawRow] = []
    for bookmaker in event.get("bookmakers", []) or []:
        title = bookmaker.get("title")
        last_update = bookmaker.get("last_update", "")
        for market in bookmaker.get("markets", []) or []:
            market_key = market.get("key")
            for outcome in market.get("outcomes", []) or []:
                rows.append(RawRow(
                    event_key=event_key, market=market_key, point=outcome.get("point"),
                    outcome_raw=outcome.get("name"), bookmaker=title, price=outcome.get("price"),
                    last_update=last_update, home_team=home_team, away_team=away_team,
                    commence_time=commence_time, event_id=event_id, league=league,
                ))
    return rows


def build_live_candidates(matches: List[FixtureMatch]) -> List[LiveCandidate]:
    """`matches` are FixtureMatch objects whose `.fixture` attribute is
    actually a LiveFixture (fixture_matching.match_fixtures_to_events only
    reads .fixture_id/.home_team/.away_team/.kickoff_utc, so a LiveFixture
    duck-types as a Fixture there). Returns at most one LiveCandidate per
    matched fixture: its single highest-signal-level, then
    highest-ranking-score real outcome. A fixture whose matched event has
    no outcome clearing even LOW produces no candidate at all."""
    out: List[LiveCandidate] = []
    for match in matches:
        # Re-tag so downstream helpers can read `.live_fixture` without
        # caring what fixture_matching itself called the attribute.
        match_with_live = FixtureMatch(fixture=match.fixture, event=match.event, confidence=match.confidence)
        match_with_live.live_fixture = match.fixture  # type: ignore[attr-defined]

        stats = ValidationStats()
        raw_rows = _extract_rows_for_event(match_with_live)
        valid_rows = validate_rows(raw_rows, stats)
        raw_counts = raw_bookmaker_row_counts(valid_rows)
        deduped_rows = dedupe_bookmaker_rows(valid_rows, stats)
        groups = group_rows(deduped_rows)
        candidates = build_value_candidates_from_groups(groups, raw_counts)

        real_candidates = [c for c in candidates if c.signal_level != SIGNAL_REJECTED]
        if not real_candidates:
            continue
        best = max(real_candidates, key=lambda c: (_LEVEL_RANK[c.signal_level], c.ranking_score))
        out.append(LiveCandidate(live_fixture=match.fixture, value_candidate=best))
    return out
