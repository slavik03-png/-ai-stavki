"""
Phase 3 -- scope The Odds API querying to only the sport_keys that
plausibly correspond to leagues/countries where a real fixture was
discovered (ai_predictions/fixtures.py), instead of blindly scanning every
active football sport_key returned by discover_football_sport_keys().

This is a textual relevance filter over real data on both sides (real
discovered fixtures' league/country names in English from API-Football,
real sport catalog title/description strings from The Odds API) -- it
never invents a sport_key and never assumes a fixture has odds; it only
narrows which sport_keys are worth asking.
"""

from __future__ import annotations

from typing import Any, Dict, List, Set

from ai_predictions.fixtures import Fixture
from ai_predictions.matching import normalize_text

#: Always queried regardless of textual match -- top leagues frequently
#: named differently enough (e.g. "EPL" vs "England - Premier League")
#: that a pure substring match would miss them, and they are cheap/likely
#: to contain a real discovered fixture often enough to be worth the call.
_ALWAYS_RELEVANT_HINTS = ("premier league", "champions league", "europa league", "la liga",
                           "serie a", "bundesliga", "ligue 1")


def select_relevant_sport_keys(
    fixtures: List[Fixture],
    sports_catalog: List[Dict[str, Any]],
) -> List[str]:
    """`sports_catalog` entries look like {"key", "title", "description"}
    (The Odds API /v4/sports shape). Returns the subset of keys whose
    title/description textually overlaps a discovered fixture's league
    name or country, or matches one of the always-relevant top leagues."""
    fixture_terms: Set[str] = set()
    for fx in fixtures:
        if fx.league_country:
            fixture_terms.add(normalize_text(fx.league_country))
        if fx.league_name:
            fixture_terms.add(normalize_text(fx.league_name))

    if not fixture_terms:
        return []

    relevant: List[str] = []
    for entry in sports_catalog:
        key = entry.get("key")
        if not key:
            continue
        haystack = normalize_text(f"{entry.get('title', '')} {entry.get('description', '')}")
        if any(hint in haystack for hint in _ALWAYS_RELEVANT_HINTS):
            relevant.append(key)
            continue
        if any(term and (term in haystack or haystack in term or _word_overlap(term, haystack)) for term in fixture_terms):
            relevant.append(key)
    return relevant


def _word_overlap(term: str, haystack: str) -> bool:
    term_words = {w for w in term.split() if len(w) > 3}
    haystack_words = set(haystack.split())
    return bool(term_words & haystack_words)
