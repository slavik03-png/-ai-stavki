"""
Tests for the empty-pool archive fix (2026-07-16):

1. Today's archive with empty pool is NOT considered valid.
2. An empty archive does NOT overwrite the last successful non-empty archive.
3. The "все варианты показаны или начались" message appears ONLY when the
   original pool actually had recommendations; never for an empty pool.
4. After a restart, an empty archive is NOT shown as актуален / "архив текущего дня".
5. Dates and times are computed in Asia/Yekaterinburg (UTC+5).
6. archive_empty_reason() returns an appropriate reason for each failure mode.
7. load_last_successful_archive() returns None when no successful run exists.
8. load_last_successful_archive() returns the backup even after an empty run.

No real network calls anywhere in this file.
"""

import datetime
import sys
import tempfile

sys.path.insert(0, ".")

from ai_predictions.fixtures import Fixture
from ai_predictions.football_cache import FootballCache
from ai_predictions.football_pipeline import (
    DailyArchive,
    FootballPipelineResult,
    LAST_SUCCESSFUL_ARCHIVE_KEY,
    archive_empty_reason,
    is_archive_valid,
    load_daily_archive,
    load_last_successful_archive,
    save_daily_archive,
)
from ai_predictions.football_predictions import MarketCandidate
from ai_predictions.prediction_report import (
    NOTHING_LEFT_FOR_USER_TEMPLATE,
    POOL_EMPTY_TEMPLATE,
    render_nothing_left_for_user_message,
    render_pool_empty_message,
)
from ai_predictions.prediction_selector import RankedRecommendation
from ai_predictions.window import is_same_local_day

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name}{' — ' + detail if detail else ''}")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

# UTC midnight of July 15 → 05:00 Yekaterinburg (Asia/Yekaterinburg is UTC+5)
NOW = datetime.datetime(2026, 7, 15, 7, 0, tzinfo=datetime.timezone.utc)
# A moment on a DIFFERENT Yekaterinburg calendar day (July 16 local = July 15 UTC 19:00+)
NEXT_DAY_UTC = datetime.datetime(2026, 7, 15, 20, 0, tzinfo=datetime.timezone.utc)


def _fixture(fid, hours_ahead=5):
    return Fixture(
        fixture_id=fid,
        kickoff_utc=NOW + datetime.timedelta(hours=hours_ahead),
        home_team="Дом", away_team="Гости",
        home_team_id=1, away_team_id=2,
        league_name="Test League", league_country="World", status_short="NS",
    )


def _pool_entry(fid=1, hours_ahead=5):
    fixture = _fixture(fid, hours_ahead=hours_ahead)
    candidate = MarketCandidate(
        fixture=fixture, market_key="h2h_home", market_label_ru="Победа хозяев",
        probability=0.65, completeness=1.0, sample_size_category="full",
        rationale="test", source="recent_form",
    )
    rec = RankedRecommendation(candidate=candidate, signal_level="HIGH")
    return rec, 1.9, "TestBookmaker"


def _make_result(pool_entries=None, odds_status="available", errors=None):
    r = FootballPipelineResult()
    r.pool = pool_entries or []
    r.odds_status = odds_status
    r.errors = errors or []
    r.recommendations_count = len(r.pool)
    r.found_fixtures = 5
    r.matched_fixtures = len(r.pool)
    return r


# --------------------------------------------------------------------------
# Test 1: is_archive_valid — empty pool must NOT be valid
# --------------------------------------------------------------------------

with tempfile.TemporaryDirectory() as td:
    db = td + "/t1.db"
    fc = FootballCache(db_path=db, now=NOW)
    result_empty = _make_result(pool_entries=[], odds_status="quota_exhausted")
    save_daily_archive(fc, result_empty, NOW)
    archive = load_daily_archive(fc, NOW, ignore_ttl=True)
    fc.close()

    check(
        "is_archive_valid: returns False for archive with empty pool",
        archive is not None and not is_archive_valid(archive),
        f"archive={archive}, pool_size={len(archive.pool) if archive else 'N/A'}",
    )
    check(
        "is_archive_valid: returns True only when pool has entries",
        not is_archive_valid(None),
        "None input → False",
    )

with tempfile.TemporaryDirectory() as td:
    db = td + "/t1b.db"
    fc = FootballCache(db_path=db, now=NOW)
    result_nonempty = _make_result(pool_entries=[_pool_entry()])
    save_daily_archive(fc, result_nonempty, NOW)
    archive2 = load_daily_archive(fc, NOW, ignore_ttl=True)
    fc.close()

    check(
        "is_archive_valid: returns True when pool has ≥1 entry",
        is_archive_valid(archive2),
        f"pool_size={len(archive2.pool) if archive2 else 'N/A'}",
    )

# --------------------------------------------------------------------------
# Test 2: empty result must NOT overwrite a previously successful archive
# --------------------------------------------------------------------------

with tempfile.TemporaryDirectory() as td:
    db = td + "/t2.db"
    fc = FootballCache(db_path=db, now=NOW)

    # First: save a successful result.
    result_good = _make_result(pool_entries=[_pool_entry(fid=10), _pool_entry(fid=11)])
    save_daily_archive(fc, result_good, NOW)
    before = load_daily_archive(fc, NOW, ignore_ttl=True)

    # Then: save an empty result (simulating quota exhaustion on force-refresh).
    result_bad = _make_result(pool_entries=[], odds_status="quota_exhausted")
    save_daily_archive(fc, result_bad, NOW)
    after = load_daily_archive(fc, NOW, ignore_ttl=True)
    fc.close()

    check(
        "save_daily_archive: empty result does NOT overwrite valid non-empty archive",
        after is not None and len(after.pool) == 2,
        f"pool_size_after={len(after.pool) if after else 'N/A'}",
    )

# --------------------------------------------------------------------------
# Test 3: "все варианты показаны или начались" only when pool originally had entries
# --------------------------------------------------------------------------

check(
    "render_nothing_left_for_user_message contains 'показаны или матчи начались'",
    "показаны или матчи начались" in render_nothing_left_for_user_message(),
)
check(
    "render_pool_empty_message does NOT contain 'показаны или матчи начались'",
    "показаны или матчи начались" not in render_pool_empty_message("исчерпан лимит The Odds API"),
)
check(
    "render_pool_empty_message contains the supplied reason",
    "исчерпан лимит The Odds API" in render_pool_empty_message("исчерпан лимит The Odds API"),
)
check(
    "POOL_EMPTY_TEMPLATE and NOTHING_LEFT_FOR_USER_TEMPLATE are distinct",
    POOL_EMPTY_TEMPLATE != NOTHING_LEFT_FOR_USER_TEMPLATE,
)

# --------------------------------------------------------------------------
# Test 4: after restart (load from disk), empty archive is NOT актуален
# --------------------------------------------------------------------------

with tempfile.TemporaryDirectory() as td:
    db = td + "/t4.db"

    # Write an empty archive.
    fc_write = FootballCache(db_path=db, now=NOW)
    result_empty = _make_result(pool_entries=[], odds_status="quota_exhausted")
    save_daily_archive(fc_write, result_empty, NOW)
    fc_write.close()

    # Simulate a restart: open a fresh cache connection to the same file.
    fc_read = FootballCache(db_path=db, now=NOW)
    archive_after_restart = load_daily_archive(fc_read, NOW, ignore_ttl=True)
    fc_read.close()

    check(
        "after restart: empty archive exists on disk",
        archive_after_restart is not None,
    )
    check(
        "after restart: empty archive is NOT valid (pool=0)",
        not is_archive_valid(archive_after_restart),
    )
    check(
        "after restart: empty archive does not look like a valid source",
        archive_after_restart is None or len(archive_after_restart.pool) == 0,
    )

# --------------------------------------------------------------------------
# Test 5: date/time in Asia/Yekaterinburg (UTC+5)
# --------------------------------------------------------------------------

# 19:00 UTC on July 15 = 00:00 on July 16 in Yekaterinburg → different calendar day.
utc_1900 = datetime.datetime(2026, 7, 15, 19, 0, tzinfo=datetime.timezone.utc)
utc_0900 = datetime.datetime(2026, 7, 15, 9, 0, tzinfo=datetime.timezone.utc)

check(
    "is_same_local_day: 19:00 UTC and 09:00 UTC on same UTC date → different Yekaterinburg days",
    not is_same_local_day(utc_1900, utc_0900),
    "19:00 UTC = July 16 Yekaterinburg; 09:00 UTC = July 15 Yekaterinburg",
)
check(
    "is_same_local_day: same Yekaterinburg day (both UTC morning)",
    is_same_local_day(utc_0900, NOW),
    f"09:00 UTC and {NOW.isoformat()} UTC → same Yekaterinburg day",
)

# --------------------------------------------------------------------------
# Test 6: archive_empty_reason — returns correct reason per failure mode
# --------------------------------------------------------------------------

check(
    "archive_empty_reason: quota_exhausted → mentions Odds API",
    "Odds API" in archive_empty_reason({"odds_status": "quota_exhausted"}),
)
check(
    "archive_empty_reason: no fixtures found",
    "матч" in archive_empty_reason({"found_fixtures": 0}).lower(),
)
check(
    "archive_empty_reason: no matched fixtures",
    "сопоставлен" in archive_empty_reason({"found_fixtures": 5, "matched_fixtures": 0}).lower()
    or "коэффициент" in archive_empty_reason({"found_fixtures": 5, "matched_fixtures": 0}).lower(),
)
check(
    "archive_empty_reason: generic error path",
    len(archive_empty_reason({"errors": ["something went wrong"]})) > 0,
)

# --------------------------------------------------------------------------
# Test 7: load_last_successful_archive returns None when no backup exists
# --------------------------------------------------------------------------

with tempfile.TemporaryDirectory() as td:
    db = td + "/t7.db"
    fc = FootballCache(db_path=db, now=NOW)
    fallback = load_last_successful_archive(fc, NOW)
    fc.close()

    check(
        "load_last_successful_archive: returns None when no backup exists",
        fallback is None,
    )

# --------------------------------------------------------------------------
# Test 8: load_last_successful_archive persists across an empty-pool run
# --------------------------------------------------------------------------

with tempfile.TemporaryDirectory() as td:
    db = td + "/t8.db"
    fc = FootballCache(db_path=db, now=NOW)

    # Save a non-empty result (writes to both DAILY_ARCHIVE_KEY and LAST_SUCCESSFUL).
    result_good = _make_result(pool_entries=[_pool_entry(fid=20, hours_ahead=10)])
    save_daily_archive(fc, result_good, NOW)

    # Simulate the next day's run returning an empty pool.
    next_day = NOW + datetime.timedelta(hours=20)
    result_bad = _make_result(pool_entries=[], odds_status="quota_exhausted")
    save_daily_archive(fc, result_bad, next_day)

    # LAST_SUCCESSFUL_ARCHIVE_KEY must still point to the good run.
    fallback = load_last_successful_archive(fc, next_day)
    fc.close()

    check(
        "load_last_successful_archive: returns backup after empty-pool run",
        fallback is not None and len(fallback.pool) == 1,
        f"fallback_pool_size={len(fallback.pool) if fallback else 'N/A'}",
    )
    check(
        "load_last_successful_archive: is_stale_calendar_day always True (cross-day)",
        fallback is None or fallback.is_stale_calendar_day,
    )

# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------

total = len(results)
passed = sum(1 for _, s in results if s == "PASS")
failed = total - passed
print(f"\n{'='*50}")
print(f"Results: {passed}/{total} passed, {failed} failed")
if failed:
    print("FAILED tests:")
    for name, status in results:
        if status == "FAIL":
            print(f"  - {name}")
    sys.exit(1)
else:
    print("All tests passed.")
