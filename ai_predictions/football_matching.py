"""
Team-name matching between The Odds API's plain team names (already the
strategy's own team names, see ai_predictions/matching.normalize_text) and
API-Football's `/teams?search=` candidates.

A "match" is only ever the API-Football candidate whose name is textually
closest to the real Odds API name, and only above TEAM_MATCH_CONFIDENCE_FLOOR
-- anything weaker is reported as unmatched rather than guessed, since
attaching one team's real statistics to a different real team would be
worse than having no statistics at all.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ai_predictions.matching import normalize_text
from ai_predictions.value_config import TEAM_MATCH_CONFIDENCE_FLOOR

#: Common club-suffix/prefix noise that differs between data sources for
#: the exact same real club (e.g. Odds API "Man United" vs API-Football
#: "Manchester United FC") -- stripped only for the similarity comparison,
#: never for display or for what gets stored/sent to the API.
_CLUB_NOISE_TOKENS = ("fc", "cf", "sc", "afc", "ac", "club", "calcio")


def _strip_club_noise(normalized_name: str) -> str:
    tokens = [t for t in normalized_name.split() if t not in _CLUB_NOISE_TOKENS]
    return " ".join(tokens) if tokens else normalized_name


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(a=a, b=b).ratio()


@dataclass
class TeamMatch:
    matched: bool
    team_id: Optional[int] = None
    matched_name: Optional[str] = None
    country: Optional[str] = None
    confidence: float = 0.0
    reason: Optional[str] = None  # set only when matched is False


def best_team_match(odds_team_name: str, candidates: List[Dict[str, Any]]) -> TeamMatch:
    """Scores every real API-Football candidate against the real Odds API
    team name and returns the best one, or an honest "no confident match"
    result. Never invents a candidate -- `candidates` must come straight
    from ApiFootballProvider.search_teams()."""
    if not candidates:
        return TeamMatch(matched=False, reason=f"API-Football не вернул ни одной команды по запросу «{odds_team_name}»")

    target = _strip_club_noise(normalize_text(odds_team_name))
    best: Optional[Dict[str, Any]] = None
    best_score = 0.0
    for candidate in candidates:
        candidate_name = candidate.get("name") or ""
        candidate_normalized = _strip_club_noise(normalize_text(candidate_name))
        score = _similarity(target, candidate_normalized)
        # Exact match after noise-stripping is unambiguous even if
        # difflib's ratio isn't a perfect 1.0 for short names.
        if candidate_normalized == target:
            score = max(score, 0.99)
        if score > best_score:
            best_score = score
            best = candidate

    if best is None or best_score < TEAM_MATCH_CONFIDENCE_FLOOR:
        return TeamMatch(
            matched=False,
            confidence=best_score,
            reason=(
                f"Нет уверенного совпадения для «{odds_team_name}» "
                f"(лучшее сходство {best_score:.2f} < {TEAM_MATCH_CONFIDENCE_FLOOR:.2f})"
            ),
        )
    return TeamMatch(
        matched=True,
        team_id=best.get("id"),
        matched_name=best.get("name"),
        country=best.get("country"),
        confidence=round(best_score, 3),
    )
