"""
Cumulative performance statistics over stored predictions.

All formulas are documented inline. Nothing here claims profitability is
guaranteed -- that disclaimer lives in tracking/report.py, which is the
only place end users see these numbers rendered as text.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from tracking.models import (
    DECISIVE_STATUSES,
    GRADED_STATUSES,
    STATUS_CANCELLED,
    STATUS_HALF_LOST,
    STATUS_HALF_WON,
    STATUS_LOST,
    STATUS_PENDING,
    STATUS_POSTPONED,
    STATUS_RETURNED,
    STATUS_UNRESOLVED,
    STATUS_VOID,
    STATUS_WON,
)

#: Below this many decisive (win/loss) results, statistics are still
#: computed but the report must warn that the sample is too small to draw
#: conclusions from. Kept for any code still using the single-threshold
#: check; the ranked HIGH/MEDIUM/LOW system uses the three-tier wording
#: below (sample_size_note) instead.
MIN_RELIABLE_SAMPLE = 20

#: Three-tier sample-size wording for the ranked signal system (settled
#: count, not decisive count -- pushes/voids still tell you the sample
#: exists even though they don't resolve win/loss).
VERY_SMALL_SAMPLE_MAX = 29     # < 30 settled: "very small sample"
PRELIMINARY_SAMPLE_MAX = 99    # 30-99 settled: "preliminary"
                                # 100+ settled: "meaningful but not conclusive"


def sample_size_note(settled_count: int) -> str:
    """Never claims profitability on a small sample -- always describes
    the sample-size ceiling honestly, even at 100+."""
    if settled_count <= VERY_SMALL_SAMPLE_MAX:
        return (
            f"⚠️ Очень маленькая выборка ({settled_count} рассчитанных) — "
            "статистике пока нельзя доверять вообще."
        )
    if settled_count <= PRELIMINARY_SAMPLE_MAX:
        return (
            f"⚠️ Предварительная выборка ({settled_count} рассчитанных) — "
            "тенденция только начинает вырисовываться, делать выводы ещё рано."
        )
    return (
        f"Значимая выборка ({settled_count} рассчитанных), но не окончательная — "
        "прошлые результаты всё равно не гарантируют будущую прибыль."
    )


def _profit(status: str, odds: float) -> float:
    if status == STATUS_WON:
        return odds - 1.0
    if status == STATUS_LOST:
        return -1.0
    if status == STATUS_HALF_WON:
        return (odds - 1.0) / 2.0
    if status == STATUS_HALF_LOST:
        return -0.5
    if status in (STATUS_RETURNED, STATUS_CANCELLED, STATUS_VOID):
        return 0.0
    return 0.0  # pending / postponed / unresolved contribute nothing yet


@dataclass
class Stats:
    total: int = 0
    settled: int = 0
    pending: int = 0
    won: int = 0
    lost: int = 0
    returned: int = 0
    half_won: int = 0
    half_lost: int = 0
    cancelled: int = 0
    postponed: int = 0
    void: int = 0
    unresolved: int = 0

    win_rate: Optional[float] = None       # won / (won+lost+half_won+half_lost) * 100
    success_rate: Optional[float] = None   # (won + 0.5*half_won) / same denominator * 100
    average_odds: Optional[float] = None
    flat_stake_profit: float = 0.0
    roi: Optional[float] = None            # flat_stake_profit / settled_count * 100
    longest_winning_streak: int = 0
    longest_losing_streak: int = 0

    sample_too_small: bool = True


def _row_status(row) -> str:
    return row["status"]


def _row_odds(row) -> float:
    return row["bookmaker_odds"]


def _row_created_at(row) -> str:
    return row["created_at"]


def compute_statistics(predictions: Sequence) -> Stats:
    """`predictions` is any sequence of sqlite3.Row (or mapping-like) objects
    with the same columns as the `predictions` table."""
    stats = Stats()
    stats.total = len(predictions)

    decisive_count = 0
    weighted_success = 0.0
    settled_odds: List[float] = []
    profit_total = 0.0
    settled_count = 0

    for row in predictions:
        status = _row_status(row)
        if status == STATUS_PENDING:
            stats.pending += 1
            continue
        if status == STATUS_WON:
            stats.won += 1
        elif status == STATUS_LOST:
            stats.lost += 1
        elif status == STATUS_RETURNED:
            stats.returned += 1
        elif status == STATUS_HALF_WON:
            stats.half_won += 1
        elif status == STATUS_HALF_LOST:
            stats.half_lost += 1
        elif status == STATUS_CANCELLED:
            stats.cancelled += 1
        elif status == STATUS_POSTPONED:
            stats.postponed += 1
        elif status == STATUS_VOID:
            stats.void += 1
        elif status == STATUS_UNRESOLVED:
            stats.unresolved += 1

        if status in GRADED_STATUSES:
            stats.settled += 1
            settled_count += 1
            odds = _row_odds(row)
            settled_odds.append(odds)
            profit_total += _profit(status, odds)

        if status in DECISIVE_STATUSES:
            decisive_count += 1
            if status == STATUS_WON:
                weighted_success += 1.0
            elif status == STATUS_HALF_WON:
                weighted_success += 0.5
            # lost / half_lost contribute 0

    if decisive_count > 0:
        stats.win_rate = round((stats.won / decisive_count) * 100.0, 2)
        stats.success_rate = round((weighted_success / decisive_count) * 100.0, 2)

    if settled_odds:
        stats.average_odds = round(sum(settled_odds) / len(settled_odds), 3)

    stats.flat_stake_profit = round(profit_total, 3)
    if settled_count > 0:
        stats.roi = round((profit_total / settled_count) * 100.0, 2)

    stats.longest_winning_streak, stats.longest_losing_streak = _compute_streaks(predictions)
    stats.sample_too_small = decisive_count < MIN_RELIABLE_SAMPLE

    return stats


def _compute_streaks(predictions: Sequence) -> "tuple[int, int]":
    """Streaks are computed over decisive results only (won/lost), in
    created_at order. Non-decisive results (pending/returned/void/half/etc.)
    are skipped -- they neither extend nor reset a streak, since they carry
    no win/loss signal of their own."""
    ordered = sorted(
        (row for row in predictions if _row_status(row) in (STATUS_WON, STATUS_LOST)),
        key=_row_created_at,
    )
    longest_win = longest_loss = 0
    current_win = current_loss = 0
    for row in ordered:
        if _row_status(row) == STATUS_WON:
            current_win += 1
            current_loss = 0
        else:
            current_loss += 1
            current_win = 0
        longest_win = max(longest_win, current_win)
        longest_loss = max(longest_loss, current_loss)
    return longest_win, longest_loss


# ---------------------------------------------------------------------------
# Breakdowns
# ---------------------------------------------------------------------------

def group_by(predictions: Sequence, key_fn: Callable) -> Dict[str, Stats]:
    buckets: Dict[str, list] = {}
    for row in predictions:
        key = key_fn(row)
        if key is None:
            key = "не указано"
        buckets.setdefault(key, []).append(row)
    return {key: compute_statistics(rows) for key, rows in buckets.items()}


def by_sport(predictions: Sequence) -> Dict[str, Stats]:
    return group_by(predictions, lambda r: r["sport"])


def by_league(predictions: Sequence) -> Dict[str, Stats]:
    return group_by(predictions, lambda r: r["league"])


def by_market_type(predictions: Sequence) -> Dict[str, Stats]:
    return group_by(predictions, lambda r: r["market_type"])


def by_recommendation_group(predictions: Sequence) -> Dict[str, Stats]:
    return group_by(predictions, lambda r: r["recommendation_group"])


def by_confidence_level(predictions: Sequence) -> Dict[str, Stats]:
    return group_by(predictions, lambda r: r["confidence_level"])


def by_model_version(predictions: Sequence) -> Dict[str, Stats]:
    return group_by(predictions, lambda r: r["model_version"])


def by_signal_level(predictions: Sequence) -> Dict[str, Stats]:
    """Per-level breakdown for the ranked HIGH/MEDIUM/LOW/REJECTED value
    system. REJECTED candidates are tracked too (Step 7) so their
    settled performance is measurable, even though they were never
    surfaced to the user as an actionable signal."""
    def _key(r):
        try:
            return r["signal_level"]
        except (KeyError, IndexError):
            return None
    return group_by(predictions, _key)


def _odds_interval(odds: float) -> str:
    if odds < 1.5:
        return "<1.50"
    if odds < 2.0:
        return "1.50-1.99"
    if odds < 3.0:
        return "2.00-2.99"
    if odds < 5.0:
        return "3.00-4.99"
    return "5.00+"


def by_odds_interval(predictions: Sequence) -> Dict[str, Stats]:
    return group_by(predictions, lambda r: _odds_interval(r["bookmaker_odds"]))


def _parse_iso(value: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def last_n_days(predictions: Sequence, days: int, now: Optional[datetime.datetime] = None) -> Stats:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=days)
    filtered = [r for r in predictions if _parse_iso(_row_created_at(r)) >= cutoff]
    return compute_statistics(filtered)


def all_time(predictions: Sequence) -> Stats:
    return compute_statistics(predictions)
