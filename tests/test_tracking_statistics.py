"""
Tests for tracking/statistics.py: cumulative stats, ROI, streaks, and
breakdown/filter helpers.
"""

import datetime
import sys

sys.path.insert(0, ".")

from tracking import statistics as stats_mod
from tracking.statistics import compute_statistics, MIN_RELIABLE_SAMPLE

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


class FakeRow(dict):
    """Mimics sqlite3.Row's __getitem__-by-column-name access."""
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def _row(status, odds=2.0, created_at="2026-07-01T00:00:00+00:00", **overrides):
    base = dict(
        status=status, bookmaker_odds=odds, created_at=created_at,
        sport="football", league="Premier League", market_type="1x2",
        recommendation_group="main", confidence_level="средняя уверенность",
        model_version="v1",
    )
    base.update(overrides)
    return FakeRow(base)


def test_basic_counts_and_profit():
    rows = [
        _row("won", odds=2.0),
        _row("lost", odds=2.0),
        _row("returned", odds=2.0),
        _row("half_won", odds=3.0),
        _row("half_lost", odds=2.0),
        _row("pending", odds=2.0),
    ]
    s = compute_statistics(rows)
    check("total counts all rows", s.total == 6)
    check("pending counted separately", s.pending == 1)
    check("settled excludes pending", s.settled == 5)
    # profit: won (2-1=1) + lost (-1) + returned (0) + half_won ((3-1)/2=1) + half_lost (-0.5) = 0.5
    check("flat stake profit matches formula", abs(s.flat_stake_profit - 0.5) < 1e-9, s.flat_stake_profit)
    # decisive = won, lost, half_won, half_lost = 4; win_rate = won/decisive = 1/4 = 25%
    check("win rate excludes returns", s.win_rate == 25.0, s.win_rate)
    # success_rate weighted = (1 + 0.5*1) / 4 = 37.5%
    check("success rate weights half outcomes", s.success_rate == 37.5, s.success_rate)
    check("roi = profit/settled*100", abs(s.roi - (0.5 / 5 * 100)) < 1e-9, s.roi)


def test_average_odds():
    rows = [_row("won", odds=2.0), _row("won", odds=3.0), _row("lost", odds=1.5)]
    s = compute_statistics(rows)
    expected = round((2.0 + 3.0 + 1.5) / 3, 3)
    check("average odds computed over settled bets", abs(s.average_odds - expected) < 1e-6, s.average_odds)


def test_streaks():
    rows = [
        _row("won", created_at="2026-07-01T00:00:00+00:00"),
        _row("won", created_at="2026-07-02T00:00:00+00:00"),
        _row("lost", created_at="2026-07-03T00:00:00+00:00"),
        _row("won", created_at="2026-07-04T00:00:00+00:00"),
        _row("won", created_at="2026-07-05T00:00:00+00:00"),
        _row("won", created_at="2026-07-06T00:00:00+00:00"),
        _row("lost", created_at="2026-07-07T00:00:00+00:00"),
        _row("lost", created_at="2026-07-08T00:00:00+00:00"),
    ]
    s = compute_statistics(rows)
    check("longest winning streak", s.longest_winning_streak == 3, s.longest_winning_streak)
    check("longest losing streak", s.longest_losing_streak == 2, s.longest_losing_streak)


def test_streaks_ignore_pushes_without_resetting():
    rows = [
        _row("won", created_at="2026-07-01T00:00:00+00:00"),
        _row("returned", created_at="2026-07-02T00:00:00+00:00"),
        _row("won", created_at="2026-07-03T00:00:00+00:00"),
    ]
    s = compute_statistics(rows)
    check("push in between decisive wins does not break streak counting",
          s.longest_winning_streak == 2, s.longest_winning_streak)


def test_sample_too_small_warning():
    small = [_row("won") for _ in range(5)]
    s_small = compute_statistics(small)
    check("small decisive sample is flagged", s_small.sample_too_small)

    big = [_row("won") for _ in range(MIN_RELIABLE_SAMPLE)]
    s_big = compute_statistics(big)
    check("sample at threshold is not flagged", not s_big.sample_too_small)


def test_breakdowns_by_market_and_confidence():
    rows = [
        _row("won", market_type="1x2", confidence_level="высокая уверенность"),
        _row("lost", market_type="1x2", confidence_level="высокая уверенность"),
        _row("won", market_type="total_goals", confidence_level="средняя уверенность"),
    ]
    by_market = stats_mod.by_market_type(rows)
    by_confidence = stats_mod.by_confidence_level(rows)
    check("breakdown by market_type has 2 buckets", len(by_market) == 2, list(by_market))
    check("1x2 bucket has 2 settled", by_market["1x2"].settled == 2)
    check("breakdown by confidence level has 2 buckets", len(by_confidence) == 2)


def test_last_n_days_filter():
    now = datetime.datetime(2026, 7, 12, tzinfo=datetime.timezone.utc)
    rows = [
        _row("won", created_at="2026-07-11T00:00:00+00:00"),  # 1 day ago
        _row("won", created_at="2026-06-01T00:00:00+00:00"),  # >30 days ago
    ]
    last_7 = stats_mod.last_n_days(rows, 7, now)
    last_30 = stats_mod.last_n_days(rows, 30, now)
    check("last 7 days excludes the old row", last_7.settled == 1, last_7.settled)
    check("last 30 days still excludes the very old row", last_30.settled == 1, last_30.settled)


def run():
    test_basic_counts_and_profit()
    test_average_odds()
    test_streaks()
    test_streaks_ignore_pushes_without_resetting()
    test_sample_too_small_warning()
    test_breakdowns_by_market_and_confidence()
    test_last_n_days_filter()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
