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

Event discovery is dynamic, not a hardcoded major-league list: every real
call fetches The Odds API's live `/sports` catalog and includes every
currently active football competition it lists (lower leagues and minor
cups included), never just a fixed set of big-name leagues. The old
hardcoded list survives only as `FOOTBALL_SPORT_KEYS`, used solely as a
fallback if the live `/sports` catalog call itself fails, so a transient
API hiccup never means zero football coverage that run.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORTS_LIST_ENDPOINT = f"{ODDS_API_BASE}/sports"
REGIONS = "eu"
ODDS_FORMAT = "decimal"

#: The Odds API's own grouping label for every football/soccer competition.
FOOTBALL_GROUP = "Soccer"

#: Fallback-only list, used exclusively when the live `/sports` discovery
#: call fails outright (network error, non-200, bad JSON) -- NOT the
#: normal source of truth for which leagues get queried any more.
FOOTBALL_SPORT_KEYS = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
]

# ---------------------------------------------------------------------------
# In-process response caching (Step 13): avoids a duplicate real request for
# the same data within the TTL window. Deliberately never caches an error --
# only a confirmed successful response is reused, so a transient failure
# always gets a fresh retry next call (see the "transient error cache
# poisoning" lesson: caching a failure as if it were a confirmed empty
# result silently hides real outages).
# ---------------------------------------------------------------------------

#: The active-sports catalog changes rarely (competitions start/end
#: seasons over days/weeks, not minutes) so it is safe to cache longer.
SPORTS_LIST_CACHE_TTL_SECONDS = 3600

#: Per-sport odds change fast enough that a short TTL is used -- long
#: enough to dedupe an accidental double-run within the same minute,
#: short enough that a real re-check a few minutes later still gets a
#: fresh price. bot.py's own 30-minute report cache sits on top of this.
EVENTS_CACHE_TTL_SECONDS = 300

_sports_list_cache: Dict[str, Any] = {}
_events_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}


def _cache_get_sports_list() -> Optional[List[Dict[str, Any]]]:
    entry = _sports_list_cache.get("entry")
    if entry and (time.monotonic() - entry["fetched_at"]) < SPORTS_LIST_CACHE_TTL_SECONDS:
        return entry["data"]
    return None


def _cache_set_sports_list(data: List[Dict[str, Any]]) -> None:
    _sports_list_cache["entry"] = {"data": data, "fetched_at": time.monotonic()}


def _cache_get_events(sport_key: str, markets: str) -> Optional[Tuple[List[Dict[str, Any]], Optional[str]]]:
    entry = _events_cache.get((sport_key, markets))
    if entry and (time.monotonic() - entry["fetched_at"]) < EVENTS_CACHE_TTL_SECONDS:
        return entry["events"], entry["credits"]
    return None


def _cache_set_events(sport_key: str, markets: str, events: List[Dict[str, Any]], credits: Optional[str]) -> None:
    _events_cache[(sport_key, markets)] = {"events": events, "credits": credits, "fetched_at": time.monotonic()}


def clear_odds_cache() -> None:
    """Test/debug helper -- production code never needs to call this."""
    _sports_list_cache.clear()
    _events_cache.clear()

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


def _fetch_one_league_uncached(
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


def _fetch_one_league(
    sport_key: str, api_key: str, markets: str
) -> "Tuple[Optional[List[Dict[str, Any]]], Optional[str], Optional[str], Optional[str]]":
    """Cached wrapper (Step 13) around the real network call: reuses a
    confirmed-successful response for the same (sport_key, markets) within
    EVENTS_CACHE_TTL_SECONDS instead of issuing a duplicate real request.
    An error is never cached -- only success -- so a transient failure
    always gets a fresh retry on the next call."""
    cached = _cache_get_events(sport_key, markets)
    if cached is not None:
        events, credits = cached
        return events, credits, None, None
    events, credits, error, body = _fetch_one_league_uncached(sport_key, api_key, markets)
    if error is None:
        _cache_set_events(sport_key, markets, events, credits)
    return events, credits, error, body


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


def fetch_active_sports(api_key: Optional[str] = None) -> "Tuple[Optional[List[Dict[str, Any]]], Optional[str]]":
    """Fetches the live catalog of active sports/competitions from The
    Odds API (GET /v4/sports) -- this is the real, current list the API
    is covering right now, not anything hardcoded. Cached for
    SPORTS_LIST_CACHE_TTL_SECONDS. Returns (sports_or_None, error_or_None);
    never caches a failed response."""
    api_key = api_key or _get_odds_api_key()
    if not api_key:
        return None, "Не найден ODDS_API_KEY"

    cached = _cache_get_sports_list()
    if cached is not None:
        return cached, None

    try:
        response = requests.get(SPORTS_LIST_ENDPOINT, params={"apiKey": api_key}, timeout=30)
    except requests.RequestException as exc:
        return None, f"Сетевая ошибка The Odds API (список видов спорта): {exc}"
    if response.status_code != 200:
        return None, f"The Odds API вернул HTTP {response.status_code} для списка видов спорта"
    try:
        data = response.json()
    except ValueError:
        return None, "The Odds API вернул некорректный JSON для списка видов спорта"

    _cache_set_sports_list(data)
    return data, None


@dataclass
class SportsDiscovery:
    """Result of discovering which football competitions to query this
    run -- always the real, current output of The Odds API's own catalog
    (or the fallback list if that catalog call itself failed)."""

    included: List[str] = field(default_factory=list)
    all_active_football: List[str] = field(default_factory=list)
    skipped: Dict[str, str] = field(default_factory=dict)
    discovery_error: Optional[str] = None
    source: str = "api"  # "api" or "fallback_hardcoded"


def discover_football_sport_keys(api_key: Optional[str] = None) -> SportsDiscovery:
    """Discovers every currently active football (soccer) competition The
    Odds API is covering right now -- lower leagues and minor cups
    included whenever the API lists them as active -- instead of a fixed
    major-league list (Step 1+2 of the production-discovery fix).

    A sport-key is excluded (and recorded with its real reason) when:
    - it is not currently active (season not running), or
    - it only offers outright/futures markets (`has_outrights`), which
      have no individual two-team match for the value engine to compare
      bookmaker prices on.

    Falls back to the previous hardcoded 7-league list ONLY if the live
    `/sports` call itself fails outright, so a transient API/network
    hiccup never means zero football coverage for the run."""
    sports, error = fetch_active_sports(api_key)
    if error or not sports:
        return SportsDiscovery(
            included=list(FOOTBALL_SPORT_KEYS),
            all_active_football=list(FOOTBALL_SPORT_KEYS),
            skipped={},
            discovery_error=error or "Пустой ответ The Odds API для списка видов спорта",
            source="fallback_hardcoded",
        )

    all_football = [s for s in sports if s.get("group") == FOOTBALL_GROUP and s.get("key")]
    included: List[str] = []
    skipped: Dict[str, str] = {}
    for s in all_football:
        key = s["key"]
        if not s.get("active", True):
            skipped[key] = "неактивен по данным The Odds API (сезон/турнир сейчас не идёт)"
            continue
        if s.get("has_outrights"):
            skipped[key] = (
                "только рынок аутрайтов (победитель турнира) — нет отдельных матчей "
                "с двумя командами для сравнения коэффициентов"
            )
            continue
        included.append(key)

    return SportsDiscovery(
        included=included,
        all_active_football=[s["key"] for s in all_football],
        skipped=skipped,
        discovery_error=None,
        source="api",
    )


@dataclass
class MultiSportFetchResult:
    events: List[Dict[str, Any]] = field(default_factory=list)
    credits_remaining: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    discovery: SportsDiscovery = field(default_factory=SportsDiscovery)
    sports_queried: List[str] = field(default_factory=list)
    sports_failed: Dict[str, str] = field(default_factory=dict)


def fetch_all_active_football_events(api_key: Optional[str] = None) -> MultiSportFetchResult:
    """Full production event-discovery entrypoint: discovers every
    currently active football competition from The Odds API (Step 1),
    fetches real odds for every one of them (Step 2), and merges
    everything into a single event pool (Step 6) -- never limited to a
    hardcoded set of major leagues."""
    api_key = api_key or _get_odds_api_key()
    discovery = discover_football_sport_keys(api_key)

    if not api_key:
        return MultiSportFetchResult(discovery=discovery, errors=["Не найден ODDS_API_KEY"])

    events, credits, fetch_errors = fetch_football_events(api_key=api_key, sport_keys=discovery.included)

    succeeded_keys = {e.get("_sport_key") for e in events}
    sports_failed: Dict[str, str] = {}
    for key in discovery.included:
        if key in succeeded_keys:
            continue
        # A league can legitimately return zero events right now (no
        # matches at all) without failing -- only count it as failed when
        # one of the collected error messages actually names this key.
        matching = [e for e in fetch_errors if re.search(rf"\b{re.escape(key)}\b", e)]
        if matching:
            sports_failed[key] = matching[0]

    sports_queried = [k for k in discovery.included if k not in sports_failed]

    return MultiSportFetchResult(
        events=events,
        credits_remaining=credits,
        errors=fetch_errors,
        discovery=discovery,
        sports_queried=sports_queried,
        sports_failed=sports_failed,
    )
