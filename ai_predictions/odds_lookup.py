"""
Strictly optional coefficient enrichment (production v3). Recommendations
are already fully built from API-Football alone by the time this module
runs (ai_predictions/football_predictions.py + prediction_selector.py) --
this module only tries, best-effort, to attach a real bookmaker price to
each chosen recommendation for display. ANY failure here (no
ODDS_API_KEY, HTTP 401, exhausted quota, network error, no matching
event, no matching outcome) must degrade to `odds=None`, never raise and
never invent a price.

Unlike earlier versions, a missing real coefficient is no longer shown as
a placeholder ("нет данных") -- the caller (football_pipeline.py) drops
that recommendation entirely instead of showing it without a real,
named bookmaker price. This module's only job is to report the truth:
either a real (price, bookmaker) pair, or nothing for that fixture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ai_predictions.fixture_matching import FixtureMatch, match_fixtures_to_events
from ai_predictions.fixtures import Fixture
from ai_predictions.matching import normalize_text
from ai_predictions.odds_client import fetch_all_active_football_events

#: Our market_key -> (Odds API market key, outcome resolver). Outcome
#: resolver receives (outcome_name_normalized, home_team, away_team,
#: point) and returns True if that raw outcome is the one we want.
_H2H_SELECTORS = {"home_win", "draw", "away_win"}
_DOUBLE_CHANCE_SELECTORS = {"double_chance_1x", "double_chance_x2"}
_TOTALS_SELECTORS = {"over_1_5": ("Over", 1.5), "over_2_5": ("Over", 2.5), "under_3_5": ("Under", 3.5)}
_BTTS_SELECTORS = {"btts_yes": "Yes", "btts_no": "No"}


@dataclass
class OddsLookupResult:
    prices_by_fixture: Dict[int, float]
    status: str  # "available" | "quota_exhausted" | "unavailable"
    detail: Optional[str] = None
    #: Which real bookmaker (The Odds API's own `title` for that price) the
    #: best price in `prices_by_fixture` actually came from -- always set
    #: together with a price, never guessed or defaulted to a generic label.
    bookmaker_by_fixture: Dict[int, str] = field(default_factory=dict)


def _quota_exhausted(errors: List[str]) -> bool:
    return any("ODDS_API_QUOTA_EXHAUSTED" in e or "401" in e for e in errors)


def _best_price_for_market(
    event: Dict[str, Any], fixture: Fixture, market_key: str,
) -> Optional[Tuple[float, str]]:
    """Returns (best_price, bookmaker_title) for the requested market, or
    None if no real bookmaker in this event actually quotes it. The
    bookmaker title is The Odds API's own `title` field for whichever
    real bookmaker offered the best price -- never a placeholder."""
    home = normalize_text(fixture.home_team)
    away = normalize_text(fixture.away_team)
    best: Optional[float] = None
    best_bookmaker: Optional[str] = None

    for bookmaker in event.get("bookmakers", []) or []:
        bookmaker_title = bookmaker.get("title") or "?"
        for market in bookmaker.get("markets", []) or []:
            odds_market_key = market.get("key")
            for outcome in market.get("outcomes", []) or []:
                name = normalize_text(outcome.get("name") or "")
                price = outcome.get("price")
                try:
                    price = float(price)
                except (TypeError, ValueError):
                    continue
                if price <= 1.0:
                    continue

                matched = False
                if market_key in _H2H_SELECTORS and odds_market_key == "h2h":
                    if market_key == "home_win" and name == home:
                        matched = True
                    elif market_key == "away_win" and name == away:
                        matched = True
                    elif market_key == "draw" and name in ("draw", "x", "ничья"):
                        matched = True
                elif market_key in _DOUBLE_CHANCE_SELECTORS and odds_market_key == "double_chance":
                    has_home = home in name
                    has_away = away in name
                    has_draw = "draw" in name or "ничья" in name or name.strip() == "x"
                    collapsed = name.replace(" ", "")
                    if market_key == "double_chance_1x" and has_home and (has_draw or "1x" in collapsed):
                        matched = True
                    if market_key == "double_chance_x2" and has_away and (has_draw or "x2" in collapsed):
                        matched = True
                elif market_key in _TOTALS_SELECTORS and odds_market_key == "totals":
                    side, point = _TOTALS_SELECTORS[market_key]
                    try:
                        outcome_point = float(outcome.get("point"))
                    except (TypeError, ValueError):
                        continue
                    if name == side.lower() and abs(outcome_point - point) < 0.01:
                        matched = True
                elif market_key in _BTTS_SELECTORS and odds_market_key == "btts":
                    if name == _BTTS_SELECTORS[market_key].lower():
                        matched = True

                if matched and (best is None or price > best):
                    best = price
                    best_bookmaker = bookmaker_title
    if best is None:
        return None
    return best, best_bookmaker


def lookup_coefficients(
    fixtures: List[Fixture],
    fixture_market_keys: Dict[int, str],
    *,
    odds_api_key: Optional[str],
    persistent_cache: Optional[Any] = None,
) -> OddsLookupResult:
    """`fixture_market_keys` maps fixture_id -> the single market_key this
    fixture's recommendation needs a price for. Returns a best-effort
    price map; a fixture absent from the returned map simply has no
    coefficient available -- never an error the caller has to handle."""
    if not odds_api_key:
        return OddsLookupResult(prices_by_fixture={}, status="unavailable", detail="Не задан ODDS_API_KEY")
    if not fixtures or not fixture_market_keys:
        return OddsLookupResult(prices_by_fixture={}, status="unavailable", detail="Нет матчей для сопоставления")

    try:
        fetch_result = fetch_all_active_football_events(api_key=odds_api_key, persistent_cache=persistent_cache)
    except Exception as exc:  # best-effort: any unexpected failure just means no coefficients
        return OddsLookupResult(prices_by_fixture={}, status="unavailable", detail=str(exc))

    if _quota_exhausted(fetch_result.errors):
        return OddsLookupResult(prices_by_fixture={}, status="quota_exhausted", detail="; ".join(fetch_result.errors[:1]))
    if not fetch_result.events:
        status = "unavailable" if fetch_result.errors else "available"
        return OddsLookupResult(prices_by_fixture={}, status=status, detail="; ".join(fetch_result.errors[:1]) or None)

    match_result = match_fixtures_to_events(fixtures, fetch_result.events)
    prices: Dict[int, float] = {}
    bookmakers: Dict[int, str] = {}
    for match in match_result.matches:
        market_key = fixture_market_keys.get(match.fixture.fixture_id)
        if not market_key:
            continue
        found = _best_price_for_market(match.event, match.fixture, market_key)
        if found is not None:
            price, bookmaker_title = found
            prices[match.fixture.fixture_id] = price
            bookmakers[match.fixture.fixture_id] = bookmaker_title

    return OddsLookupResult(prices_by_fixture=prices, bookmaker_by_fixture=bookmakers, status="available")
