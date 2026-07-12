"""
Real Odds API access for the AI predictions feature.

Deliberately separate from bot.fetch_odds(): that function flattens
bookmaker/market/outcome rows for the CSV export buttons, discarding the
structure (which bookmaker offered which full set of outcomes) that the
selection engine needs for margin removal and multi-bookmaker consensus.
This module keeps the raw event/bookmaker/market/outcome tree.

Football-only in this first version (see ai_predictions/__init__.py for
why). The sport-key list is intentionally duplicated from bot.py rather
than imported from it, so this package never imports bot.py (see
tests/test_ai_predictions_isolation.py).
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
REGIONS = "eu"
ODDS_FORMAT = "decimal"

#: Same football leagues bot.py's SPORTS["football"] tracks today.
FOOTBALL_SPORT_KEYS = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
]

#: Requested in priority order. The Odds API can reject a subset of these
#: for a given plan/endpoint with HTTP 422 and a message naming exactly
#: which markets are unsupported (e.g. "Markets not supported by this
#: endpoint: btts, double_chance, draw_no_bet, team_totals") -- when that
#: happens we parse the message, drop only the named markets, and retry
#: once with the remainder, so a single unavailable market never costs us
#: the others (in particular h2h/totals/spreads, which is what the value
#: -detection strategy actually trades). If parsing fails or the retry
#: still errors, we fall back to the minimal, near-universally-supported
#: set. Whatever markets are still missing from the response are just
#: absent from `event["bookmakers"][...]["markets"]` -- never invented.
PREFERRED_MARKETS = "h2h,totals,spreads,btts,team_totals,draw_no_bet,double_chance"
FALLBACK_MARKETS = "h2h,totals,spreads"

_UNSUPPORTED_MARKETS_RE = re.compile(r"not supported by this endpoint:\s*([a-z0-9_,\s]+)", re.IGNORECASE)


def _parse_unsupported_markets(error_body: str) -> Optional[List[str]]:
    match = _UNSUPPORTED_MARKETS_RE.search(error_body or "")
    if not match:
        return None
    return [m.strip() for m in match.group(1).split(",") if m.strip()]


def _get_odds_api_key() -> Optional[str]:
    return os.getenv("ODDS_API_KEY")


def _fetch_one_league(
    sport_key: str, api_key: str, markets: str
) -> "Tuple[Optional[List[Dict[str, Any]]], Optional[str], Optional[str], Optional[str]]":
    """Returns (events_or_None, credits_remaining_header_or_None,
    error_or_None, raw_error_body_or_None). The raw body is kept separate
    from the human-facing error message so callers can parse a structured
    "unsupported markets" list out of it without any guessing."""
    url = f"{ODDS_API_BASE}/sports/{sport_key}/odds"
    params = {
        "apiKey": api_key,
        "regions": REGIONS,
        "markets": markets,
        "oddsFormat": ODDS_FORMAT,
        "dateFormat": "iso",
    }
    try:
        response = requests.get(url, params=params, timeout=30)
    except requests.RequestException as exc:
        return None, None, f"Сетевая ошибка The Odds API ({sport_key}): {exc}", None
    credits = response.headers.get("x-requests-remaining")
    if response.status_code != 200:
        body = response.text
        return None, credits, f"The Odds API вернул HTTP {response.status_code} для {sport_key}", body
    try:
        events = response.json()
    except ValueError:
        return None, credits, f"The Odds API вернул некорректный JSON для {sport_key}", None
    return events, credits, None, None


def fetch_football_events(
    api_key: Optional[str] = None,
    sport_keys: Optional[List[str]] = None,
) -> "Tuple[List[Dict[str, Any]], Optional[str], List[str]]":
    """Fetches raw event JSON (full bookmaker/market/outcome tree, not
    flattened) for every football league. Returns
    (events, credits_remaining, per_league_errors). A single league failing
    never aborts the others -- its error is recorded and it is skipped."""
    api_key = api_key or _get_odds_api_key()
    sport_keys = sport_keys if sport_keys is not None else FOOTBALL_SPORT_KEYS

    if not api_key:
        return [], None, ["Не найден ODDS_API_KEY"]

    all_events: List[Dict[str, Any]] = []
    credits_remaining: Optional[str] = None
    errors: List[str] = []

    for sport_key in sport_keys:
        events, credits, error, body = _fetch_one_league(sport_key, api_key, PREFERRED_MARKETS)
        if error:
            unsupported = _parse_unsupported_markets(body or "")
            if unsupported:
                # The API told us exactly which markets it will not serve
                # here -- drop only those and retry with the rest, so we
                # never lose h2h/totals/spreads just because an unrelated
                # market (e.g. team_totals) is unavailable on this plan.
                remaining = [m for m in PREFERRED_MARKETS.split(",") if m not in unsupported]
                retry_markets = ",".join(remaining) if remaining else FALLBACK_MARKETS
                events, credits, error, body = _fetch_one_league(sport_key, api_key, retry_markets)
            if error:
                # Last resort: the minimal, near-universally-supported set.
                events, credits, error, body = _fetch_one_league(sport_key, api_key, FALLBACK_MARKETS)
        if credits is not None:
            credits_remaining = credits
        if error:
            errors.append(error)
            continue
        for event in events or []:
            event["_sport_key"] = sport_key
            all_events.append(event)

    return all_events, credits_remaining, errors
