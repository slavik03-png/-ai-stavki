"""
Real regression test for the reported bug: an archive generated late in
the Yekaterinburg evening must NOT be replayed after local midnight, even
though it is still well within the rolling 24h TTL.
"""
import datetime
import tempfile

from ai_predictions.football_cache import FootballCache
from ai_predictions.football_pipeline import (
    DAILY_ARCHIVE_KEY, FootballPipelineResult, load_daily_archive, save_daily_archive,
)

# 2026-07-14 23:50 Yekaterinburg (UTC+5) = 2026-07-14 18:50 UTC
generated_at = datetime.datetime(2026, 7, 14, 18, 50, tzinfo=datetime.timezone.utc)
# 2026-07-15 00:10 Yekaterinburg = 2026-07-14 19:10 UTC -- only 20 minutes
# later, well inside the 24h TTL, but a NEW Yekaterinburg calendar day.
now_after_midnight = datetime.datetime(2026, 7, 14, 19, 10, tzinfo=datetime.timezone.utc)

path = tempfile.mktemp()
cache = FootballCache(db_path=path, now=generated_at)
result = FootballPipelineResult(telegram_messages=["🤖 Прогноз за вчера"], recommendations_count=1)
save_daily_archive(cache, result, generated_at)

# Sanity: within the SAME calendar day, the archive must still be served.
same_day_probe = generated_at + datetime.timedelta(minutes=5)
archive_same_day = load_daily_archive(cache, same_day_probe)
assert archive_same_day is not None, "archive must be reusable within the same Yekaterinburg day"
print("PASS: same-day reuse still works")

# The actual bug: after local midnight, the (still <24h-old) archive must
# be rejected so a fresh run is triggered instead of replaying yesterday.
archive_after_midnight = load_daily_archive(cache, now_after_midnight)
assert archive_after_midnight is None, (
    "BUG: an archive from a previous Yekaterinburg calendar day was served as fresh"
)
print("PASS: archive from a previous calendar day is rejected after local midnight")

# The "refresh in progress -> fall back to stale" path must ALSO refuse a
# cross-day archive (ignore_ttl=True alone must not resurrect yesterday).
stale_fallback = load_daily_archive(cache, now_after_midnight, ignore_ttl=True)
assert stale_fallback is None, "ignore_ttl must not bypass the calendar-day gate"
print("PASS: ignore_ttl fallback also refuses a previous-day archive")

# /status is allowed to see it (diagnostics only) and must flag it stale.
status_view = load_daily_archive(cache, now_after_midnight, ignore_ttl=True, allow_stale_calendar_day=True)
assert status_view is not None and status_view.is_stale_calendar_day is True
print("PASS: /status diagnostics can still see it, correctly flagged as stale_calendar_day")

# Now simulate the real fix path: a fresh run for the new day gets saved,
# and immediately becomes the one served.
new_result = FootballPipelineResult(telegram_messages=["🤖 Прогноз на сегодня"], recommendations_count=2)
save_daily_archive(cache, new_result, now_after_midnight)
fresh = load_daily_archive(cache, now_after_midnight)
assert fresh is not None and fresh.messages == ["🤖 Прогноз на сегодня"]
print("PASS: a fresh same-day archive built after midnight is served correctly")

cache.close()
print("\nALL MIDNIGHT-CROSSOVER CHECKS PASSED")
