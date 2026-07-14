"""
Phase 4 -- matching real The Odds API bookmaker events to real API-Football
fixtures discovered by ai_predictions/fixtures.py.

A match requires BOTH team names to be confidently identified as the same
real club (normalized-name similarity, reusing the same building blocks as
ai_predictions/football_matching.py) AND the two providers' independently
reported kickoff times to agree within FIXTURE_KICKOFF_TOLERANCE_MINUTES.
If a fixture or event has more than one plausible counterpart above the
confidence floor with closely competing scores, the pair is reported as
ambiguous and dropped rather than guessed -- attaching the wrong real
fixture's statistics to an event would be worse than skipping it.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ai_predictions.fixtures import Fixture
from ai_predictions.football_matching import _similarity, _strip_club_noise
from ai_predictions.matching import normalize_text
from ai_predictions.value_config import (
    FIXTURE_KICKOFF_TOLERANCE_MINUTES,
    FIXTURE_MATCH_CONFIDENCE_FLOOR,
)
from ai_predictions.window import parse_commence_time

#: If the best and second-best candidate scores for the same fixture are
#: closer than this, the match is ambiguous rather than confident.
_AMBIGUITY_GAP = 0.05


@dataclass
class FixtureMatch:
    fixture: Fixture
    event: Dict[str, Any]
    confidence: float


@dataclass
class FixtureMatchResult:
    matches: List[FixtureMatch] = field(default_factory=list)
    unmatched_fixtures: List[Fixture] = field(default_factory=list)
    unmatched_events: List[Dict[str, Any]] = field(default_factory=list)
    ambiguous_fixtures: List[Fixture] = field(default_factory=list)


def _team_score(a: str, b: str) -> float:
    na = _strip_club_noise(normalize_text(a))
    nb = _strip_club_noise(normalize_text(b))
    if na == nb:
        return 0.99
    return _similarity(na, nb)


def _pair_score(fixture: Fixture, event: Dict[str, Any]) -> Optional[float]:
    home_score = _team_score(fixture.home_team, event.get("home_team") or "")
    away_score = _team_score(fixture.away_team, event.get("away_team") or "")
    if home_score < FIXTURE_MATCH_CONFIDENCE_FLOOR or away_score < FIXTURE_MATCH_CONFIDENCE_FLOOR:
        return None
    event_kickoff = parse_commence_time(event.get("commence_time"))
    if event_kickoff is None:
        return None
    delta_minutes = abs((event_kickoff - fixture.kickoff_utc).total_seconds()) / 60.0
    if delta_minutes > FIXTURE_KICKOFF_TOLERANCE_MINUTES:
        return None
    return (home_score + away_score) / 2.0


def match_fixtures_to_events(
    fixtures: List[Fixture],
    events: List[Dict[str, Any]],
) -> FixtureMatchResult:
    result = FixtureMatchResult()

    # Score every plausible (fixture, event) pair once.
    scored: List[Tuple[float, Fixture, Dict[str, Any]]] = []
    for fixture in fixtures:
        for event in events:
            score = _pair_score(fixture, event)
            if score is not None:
                scored.append((score, fixture, event))
    scored.sort(key=lambda item: item[0], reverse=True)

    ambiguous_fixture_ids: set = set()

    # Per-fixture candidate lists: a fixture with two plausible events
    # whose scores are too close to call is ambiguous -- dropped before
    # the greedy assignment pass so it can never win a pair by chance
    # ordering.
    by_fixture: Dict[int, List[float]] = {}
    for score, fixture, event in scored:
        by_fixture.setdefault(fixture.fixture_id, []).append(score)
    for fixture_id, scores in by_fixture.items():
        scores.sort(reverse=True)
        if len(scores) > 1 and (scores[0] - scores[1]) < _AMBIGUITY_GAP:
            ambiguous_fixture_ids.add(fixture_id)
    for fixture in fixtures:
        if fixture.fixture_id in ambiguous_fixture_ids:
            result.ambiguous_fixtures.append(fixture)

    # Greedy global assignment: highest-confidence pairs first, each
    # fixture and each event used at most once.
    used_fixtures: set = set()
    used_events: set = set()
    for score, fixture, event in scored:
        if fixture.fixture_id in ambiguous_fixture_ids:
            continue
        if fixture.fixture_id in used_fixtures:
            continue
        event_id = event.get("id")
        if event_id in used_events:
            continue
        used_fixtures.add(fixture.fixture_id)
        used_events.add(event_id)
        result.matches.append(FixtureMatch(fixture=fixture, event=event, confidence=round(score, 3)))

    matched_ids = used_fixtures
    for fixture in fixtures:
        if fixture.fixture_id not in matched_ids and fixture.fixture_id not in ambiguous_fixture_ids:
            if fixture not in result.unmatched_fixtures:
                result.unmatched_fixtures.append(fixture)

    for event in events:
        if event.get("id") not in used_events:
            result.unmatched_events.append(event)

    return result
