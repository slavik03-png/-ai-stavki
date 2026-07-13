"""
Cross-bookmaker row extraction, validation, normalization and grouping for
the value-detection strategy. Pulled out of value_engine.py into its own
module so the matching/grouping mechanics (this file) are fully separable
from the value-detection math (leave-one-out consensus, edge, threshold --
still in value_engine.py). This module never computes a probability or an
edge -- it only turns a raw Odds API event into clean, validated,
deduplicated rows and stable groups.

Pipeline stages (see tests/test_ai_predictions_matching.py):
    raw event -> extract_rows -> validate_rows -> dedupe_bookmaker_rows
    -> group_rows -> (event_key, market, point, outcome) groups with a
    real bookmaker count each.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

#: Markets this strategy actively trades. Any other market key seen in a
#: real bookmaker response (e.g. "h2h_lay" from exchange-style bookmakers)
#: is deliberately NOT merged into these and NOT silently dropped -- it is
#: counted and reported as an explicit "unsupported market" so a zero
#: -candidates run is never mistaken for a hidden parsing failure.
SUPPORTED_MARKET_KEYS = frozenset({"h2h", "totals", "spreads", "double_chance", "draw_no_bet"})

#: Canonical outcome labels used for ALL comparisons/grouping (never the
#: raw bookmaker-supplied string, which can differ in case/whitespace/
#: unicode between bookmakers for the exact same real outcome).
HOME, DRAW, AWAY, OVER, UNDER = "HOME", "DRAW", "AWAY", "OVER", "UNDER"


def normalize_text(value: Optional[str]) -> str:
    """Unicode-normalizes (NFKC), collapses/strips whitespace, and
    casefolds -- for COMPARISON only, never for display."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = " ".join(value.split())
    return value.casefold()


def normalize_point(value: Any) -> Optional[float]:
    """Normalizes a point/line to a float rounded to 2 decimals so 2.5,
    2.50, "2.5", -2, -2.0 and -2.00 all compare equal. Returns None for a
    missing or non-numeric point (never invented)."""
    if value is None or value == "":
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def normalize_price(value: Any) -> Optional[float]:
    """Returns a validated numeric price, or None if missing/non-numeric/
    <=1 (a real decimal price can never be 1.0 or below)."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price <= 1.0:
        return None
    return price


def build_event_key(sport_key: Optional[str], commence_time: Optional[str],
                     home_team: Optional[str], away_team: Optional[str]) -> Tuple[str, str, str, str]:
    """Stable event key from sport + UTC start time + normalized team names
    ONLY -- deliberately never includes a bookmaker, so the same real event
    quoted by 20 different bookmakers always maps to exactly one key."""
    return (
        normalize_text(sport_key),
        normalize_text(commence_time),
        normalize_text(home_team),
        normalize_text(away_team),
    )


def canonical_outcome(market_key: str, outcome_name: str, home_team: str, away_team: str) -> Optional[str]:
    """Maps a bookmaker's raw outcome string to a canonical label so
    comparisons never depend on a bookmaker's exact spelling/case/unicode
    of a team name. Returns None if the outcome cannot be identified at
    all (never guessed)."""
    name = normalize_text(outcome_name)
    home = normalize_text(home_team)
    away = normalize_text(away_team)
    if market_key in ("h2h", "spreads", "draw_no_bet"):
        if name == home:
            return HOME
        if name == away:
            return AWAY
        if market_key == "h2h" and name in ("draw", "x", "ничья"):
            return DRAW
        return None
    if market_key == "totals":
        if name in ("over", "больше"):
            return OVER
        if name in ("under", "меньше"):
            return UNDER
        return None
    if market_key == "double_chance":
        has_home = home in name
        has_away = away in name
        has_draw = "draw" in name or "ничья" in name or name.strip() == "x"
        collapsed = name.replace(" ", "")
        if has_home and has_away:
            return "HOME_OR_AWAY"
        if has_home and (has_draw or "1x" in collapsed):
            return "HOME_OR_DRAW"
        if has_away and (has_draw or "x2" in collapsed):
            return "DRAW_OR_AWAY"
        return None
    return None


@dataclass
class RawRow:
    event_key: Tuple[str, str, str, str]
    market: str
    point: Optional[float]
    outcome_raw: str
    bookmaker: str
    price: Any
    last_update: str
    # Denormalized event display fields, carried along for candidate output.
    home_team: str
    away_team: str
    commence_time: str
    event_id: str
    league: Optional[str]


@dataclass
class ValidationStats:
    rows_total: int = 0
    rows_valid: int = 0
    rejected_missing_bookmaker: int = 0
    rejected_missing_market: int = 0
    rejected_missing_outcome: int = 0
    rejected_invalid_price: int = 0
    rejected_missing_point: int = 0
    rejected_unsupported_market: int = 0
    unsupported_markets_seen: Dict[str, int] = field(default_factory=dict)
    duplicate_bookmaker_rows: int = 0


def extract_rows(event: Dict[str, Any], *, event_id: str, league: Optional[str]) -> List[RawRow]:
    """Flattens one raw Odds API event into RawRow entries -- one per
    (bookmaker, market, outcome). Never filters or validates here; that is
    validate_rows' job, so every real row is visible to diagnostics."""
    home_team = event.get("home_team", "")
    away_team = event.get("away_team", "")
    commence_time = event.get("commence_time", "")
    event_key = build_event_key(event.get("_sport_key"), commence_time, home_team, away_team)

    rows: List[RawRow] = []
    for bookmaker in event.get("bookmakers", []) or []:
        title = bookmaker.get("title")
        last_update = bookmaker.get("last_update", "")
        for market in bookmaker.get("markets", []) or []:
            market_key = market.get("key")
            for outcome in market.get("outcomes", []) or []:
                rows.append(RawRow(
                    event_key=event_key,
                    market=market_key,
                    point=outcome.get("point"),
                    outcome_raw=outcome.get("name"),
                    bookmaker=title,
                    price=outcome.get("price"),
                    last_update=last_update,
                    home_team=home_team,
                    away_team=away_team,
                    commence_time=commence_time,
                    event_id=event_id,
                    league=league,
                ))
    return rows


def validate_rows(rows: List[RawRow], stats: ValidationStats) -> List[RawRow]:
    """Applies the exact reject rules the strategy requires (section 6):
    missing bookmaker/market/outcome, non-numeric or <=1 price, missing
    point for totals/spreads. Everything else passes through untouched.
    Rows for markets this strategy does not trade (e.g. h2h_lay) are
    counted separately as "unsupported market", never silently merged or
    dropped without a trace."""
    valid: List[RawRow] = []
    for row in rows:
        stats.rows_total += 1
        if not row.bookmaker:
            stats.rejected_missing_bookmaker += 1
            continue
        if not row.market:
            stats.rejected_missing_market += 1
            continue
        if row.market not in SUPPORTED_MARKET_KEYS:
            stats.rejected_unsupported_market += 1
            stats.unsupported_markets_seen[row.market] = stats.unsupported_markets_seen.get(row.market, 0) + 1
            continue
        if not row.outcome_raw:
            stats.rejected_missing_outcome += 1
            continue
        price = normalize_price(row.price)
        if price is None:
            stats.rejected_invalid_price += 1
            continue
        point = normalize_point(row.point)
        if row.market in ("totals", "spreads") and point is None:
            stats.rejected_missing_point += 1
            continue
        row.price = price
        row.point = point
        valid.append(row)
    stats.rows_valid = len(valid)
    return valid


def dedupe_bookmaker_rows(rows: List[RawRow], stats: ValidationStats) -> List[RawRow]:
    """Within one (event, market, point, canonical outcome), a single
    bookmaker must contribute at most one price. If the same bookmaker
    appears more than once for the exact same real selection, the row
    with the newest last_update wins; the rest are counted as duplicates,
    never silently averaged or arbitrarily picked."""
    best: Dict[Tuple[Any, ...], RawRow] = {}
    for row in rows:
        canonical = canonical_outcome(row.market, row.outcome_raw, row.home_team, row.away_team)
        if canonical is None:
            continue
        key = (row.event_key, row.market, row.point, canonical, normalize_text(row.bookmaker))
        current = best.get(key)
        if current is None or row.last_update > current.last_update:
            if current is not None:
                stats.duplicate_bookmaker_rows += 1
            best[key] = row
        else:
            stats.duplicate_bookmaker_rows += 1
    return list(best.values())


def raw_bookmaker_row_counts(rows: List[RawRow]) -> Dict[Tuple[Any, ...], int]:
    """Counts real quote rows per (event_key, market, point, canonical
    outcome) BEFORE bookmaker-level deduplication -- used only as a Step 3
    confidence-safeguard diagnostic ("total bookmaker count before
    deduplication"), never for the actual matching/grouping/value math,
    which always runs on deduped rows. Call with the *validated* (not yet
    deduped) row list."""
    counts: Dict[Tuple[Any, ...], int] = {}
    for row in rows:
        canonical = canonical_outcome(row.market, row.outcome_raw, row.home_team, row.away_team)
        if canonical is None:
            continue
        key = (row.event_key, row.market, _grouping_point(row.market, row.point), canonical)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _grouping_point(market: str, point: Optional[float]) -> Optional[float]:
    """The key used to group rows into one market instance. For two-sided
    handicaps ("spreads") the API quotes the same real line from both
    sides with opposite signs (home -1.5 / away +1.5) -- grouping by the
    *signed* point would wrongly split one real line into two one-sided
    groups with nothing to compare against for margin removal, so spreads
    group by magnitude instead. Every other market's point is already
    symmetric between outcomes (e.g. a 2.5 totals line has no sign), so it
    groups by the literal point unchanged."""
    if point is None:
        return None
    return abs(point) if market == "spreads" else point


@dataclass
class MarketGroup:
    event_key: Tuple[str, str, str, str]
    event_id: str
    league: Optional[str]
    home_team: str
    away_team: str
    commence_time: str
    market: str
    point: Optional[float]
    #: canonical outcome -> list of (bookmaker, price, original_signed_point)
    outcomes: Dict[str, List[Tuple[str, float, Optional[float]]]] = field(default_factory=dict)

    def bookmaker_count(self, outcome: str) -> int:
        return len({bm for bm, _, _ in self.outcomes.get(outcome, [])})


def group_rows(rows: List[RawRow]) -> Dict[Tuple[Any, ...], MarketGroup]:
    """Groups deduplicated, validated rows by (event_key, market, point) --
    never by bookmaker, per spec section 3. Each group tracks per-outcome
    (bookmaker, price, original signed point) triples so the caller can
    count real independent bookmakers per outcome (section 8) before
    deciding anything is "matched" (section 9), while still knowing the
    real signed handicap for each side when building a spreads candidate."""
    groups: Dict[Tuple[Any, ...], MarketGroup] = {}
    for row in rows:
        canonical = canonical_outcome(row.market, row.outcome_raw, row.home_team, row.away_team)
        if canonical is None:
            continue
        grouping_point = _grouping_point(row.market, row.point)
        group_key = (row.event_key, row.market, grouping_point)
        group = groups.get(group_key)
        if group is None:
            group = MarketGroup(
                event_key=row.event_key, event_id=row.event_id, league=row.league,
                home_team=row.home_team, away_team=row.away_team, commence_time=row.commence_time,
                market=row.market, point=grouping_point,
            )
            groups[group_key] = group
        group.outcomes.setdefault(canonical, []).append((row.bookmaker, row.price, row.point))
    return groups
