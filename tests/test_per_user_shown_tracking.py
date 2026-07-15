"""
Covers per-user shown-tracking & pool re-selection (2026-07-15 task):
each Telegram user gets their own "already shown" history against the
SAME shared daily pool, so pressing "🤖 Прогнозы ИИ" again never repeats
a pick already shown to that specific user earlier the same
(Yekaterinburg) calendar day, while a different user is unaffected.
Also covers the admin-only /reset_shown command and the exact required
"nothing left" message. No real API calls happen in this file at all --
FootballCache is a real (tempfile) SQLite db, but nothing here touches
API-Football or The Odds API.
"""

import asyncio
import datetime
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, ".")

import bot
from ai_predictions.fixtures import Fixture
from ai_predictions.football_cache import FootballCache
from ai_predictions.football_pipeline import (
    FootballPipelineResult,
    load_daily_archive,
    reselect_from_archive,
    save_daily_archive,
)
from ai_predictions.football_predictions import MarketCandidate
from ai_predictions.prediction_report import render_nothing_left_for_user_message
from ai_predictions.prediction_selector import MAX_RECOMMENDATIONS, RankedRecommendation
from ai_predictions.window import local_date_str
from analytics.storage import AnalyticsStorage
from tracking.storage import TrackingStorage

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


NOW = datetime.datetime(2026, 7, 15, 12, 0, tzinfo=datetime.timezone.utc)

USER_A = 111
USER_B = 222
ADMIN_ID = 999


def make_entry(fixture_id, kickoff_utc, probability=0.65, market_key="h2h_home", home="Дом", away="Гости"):
    fixture = Fixture(
        fixture_id=fixture_id, kickoff_utc=kickoff_utc, home_team=home, away_team=away,
        home_team_id=1, away_team_id=2, league_name="Test League",
        league_country="World", status_short="NS",
    )
    candidate = MarketCandidate(
        fixture=fixture, market_key=market_key, market_label_ru="Победа хозяев",
        probability=probability, completeness=1.0, sample_size_category="full",
        rationale="test", source="recent_form",
    )
    rec = RankedRecommendation(candidate=candidate, signal_level="HIGH")
    return rec, 1.9, "TestBookmaker"


def run():
    # 7 real candidates, well beyond the min lead time, so the pool is
    # bigger than MAX_RECOMMENDATIONS (5) -- enough to prove exclusion
    # surfaces the next-best entries rather than being padded/faked.
    pool = [
        make_entry(100 + i, NOW + datetime.timedelta(hours=3, minutes=i), probability=0.9 - i * 0.02)
        for i in range(7)
    ]
    db_path = tempfile.mktemp()
    football_cache = FootballCache(db_path=db_path, now=NOW)
    result = FootballPipelineResult(pool=pool, found_fixtures=len(pool), analysed_fixtures=len(pool), matched_fixtures=len(pool))
    save_daily_archive(football_cache, result, NOW)
    archive = load_daily_archive(football_cache, NOW)

    tracking_db = tempfile.mktemp()
    analytics_db = tempfile.mktemp()
    storage = TrackingStorage(db_path=tracking_db)
    analytics_storage = AnalyticsStorage(db_path=analytics_db, now=NOW)

    # -- (1) morning press for user A shows the first batch ----------------
    messages_a1, entries_a1, saved_a1, dup_a1 = reselect_from_archive(
        archive.pool, archive.diagnostics, NOW,
        storage=storage, analytics_storage=analytics_storage,
        football_cache=football_cache, telegram_user_id=USER_A,
    )
    ids_a1 = [e[0].candidate.fixture.fixture_id for e in entries_a1]
    check("first press for user A returns the top 5 by rank", ids_a1 == [100 + i for i in range(MAX_RECOMMENDATIONS)], ids_a1)
    check("first press for user A persists the newly-shown picks", saved_a1 == MAX_RECOMMENDATIONS and dup_a1 == 0, (saved_a1, dup_a1))

    # -- (2) later same-day press from user A excludes started matches
    # AND already-shown ones, surfacing the next best real candidates ------
    later_now = NOW + datetime.timedelta(hours=4)  # all 7 pool fixtures have "started" by kickoff comparison time shift below
    # Re-run with a pool where the first 5 (already shown) are now started,
    # and the remaining 2 are still safely in the future relative to later_now.
    pool2 = [
        make_entry(100 + i, later_now - datetime.timedelta(minutes=5), probability=0.9 - i * 0.02)
        if i < 5 else
        make_entry(100 + i, later_now + datetime.timedelta(hours=2), probability=0.9 - i * 0.02)
        for i in range(7)
    ]
    messages_a2, entries_a2, saved_a2, dup_a2 = reselect_from_archive(
        pool2, archive.diagnostics, later_now,
        storage=storage, analytics_storage=analytics_storage,
        football_cache=football_cache, telegram_user_id=USER_A,
    )
    ids_a2 = [e[0].candidate.fixture.fixture_id for e in entries_a2]
    check("later press for user A excludes started AND already-shown picks, surfaces the rest",
          ids_a2 == [105, 106], ids_a2)
    check("later press for user A persists only the newly-surfaced picks", saved_a2 == 2 and dup_a2 == 0, (saved_a2, dup_a2))

    # -- (3) a different user is unaffected by user A's shown-history ------
    messages_b1, entries_b1, saved_b1, dup_b1 = reselect_from_archive(
        pool2, archive.diagnostics, later_now,
        storage=storage, analytics_storage=analytics_storage,
        football_cache=football_cache, telegram_user_id=USER_B,
    )
    ids_b1 = [e[0].candidate.fixture.fixture_id for e in entries_b1]
    check("a different user sees the still-startable picks regardless of user A's shown-history",
          ids_b1 == [105, 106], ids_b1)

    # -- (4) fewer than 3 remaining still shows all of them (never padded) --
    # User B has now been shown 105 and 106 too; only fixtures 100-104
    # remain in pool2 but those have already started for both users, so
    # nothing at all is left -- covered by (5) below. To test "fewer than
    # 3, show them all" honestly, build a fresh tiny pool with exactly 2
    # still-eligible, never-shown candidates for a brand new user.
    tiny_pool = [make_entry(500 + i, NOW + datetime.timedelta(hours=3), probability=0.6) for i in range(2)]
    messages_c1, entries_c1, saved_c1, dup_c1 = reselect_from_archive(
        tiny_pool, archive.diagnostics, NOW,
        storage=storage, analytics_storage=analytics_storage,
        football_cache=football_cache, telegram_user_id=333,
    )
    check("fewer than 3 remaining real candidates are shown without padding", len(entries_c1) == 2, len(entries_c1))

    # -- (5) zero remaining (all shown) yields the exact required message --
    messages_a3, entries_a3, saved_a3, dup_a3 = reselect_from_archive(
        pool2, archive.diagnostics, later_now,
        storage=storage, analytics_storage=analytics_storage,
        football_cache=football_cache, telegram_user_id=USER_A,
    )
    check("nothing left for user A yields zero entries", entries_a3 == [], entries_a3)
    check("nothing left for user A yields the exact required Russian message",
          messages_a3 == [render_nothing_left_for_user_message()], messages_a3)

    # -- (6) /reset_shown restores previously-shown-but-still-valid picks --
    cleared = football_cache.clear_shown_for_user(local_date_str(later_now), USER_A)
    check("clear_shown_for_user reports how many rows were cleared", cleared > 0, cleared)
    messages_a4, entries_a4, saved_a4, dup_a4 = reselect_from_archive(
        pool2, archive.diagnostics, later_now,
        storage=storage, analytics_storage=analytics_storage,
        football_cache=football_cache, telegram_user_id=USER_A,
    )
    ids_a4 = [e[0].candidate.fixture.fixture_id for e in entries_a4]
    check("after /reset_shown, previously-shown-but-still-valid picks resurface for user A",
          ids_a4 == [105, 106], ids_a4)

    # -- (7) callers that never pass a telegram_user_id/football_cache keep
    # the exact prior (non-per-user) behaviour, including the OLD generic
    # empty-pool message rather than the new per-user one. -----------------
    messages_legacy, entries_legacy, saved_legacy, dup_legacy = reselect_from_archive(
        [], archive.diagnostics, NOW, storage=storage, analytics_storage=analytics_storage,
    )
    check("legacy call (no telegram_user_id) is unaffected by per-user tracking",
          entries_legacy == [] and messages_legacy != [render_nothing_left_for_user_message()], messages_legacy)

    storage.close()
    analytics_storage.close()
    football_cache.close()


async def bot_admin_tests():
    ctx = MagicMock()

    def make_update(user_id):
        update = MagicMock()
        update.message = AsyncMock()
        update.effective_user = MagicMock(id=user_id)
        return update

    bot.ADMIN_TELEGRAM_IDS = {ADMIN_ID}

    path = tempfile.mktemp()
    bot._open_football_cache = lambda now: FootballCache(db_path=path, now=now)

    # Seed some shown-history for the admin.
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    seed_cache = bot._open_football_cache(now_dt)
    seed_cache.mark_shown(local_date_str(now_dt), ADMIN_ID, [(1, "h2h_home"), (2, "h2h_away")])
    seed_cache.close()

    # Non-admin is refused.
    update = make_update(1)
    await bot.reset_shown_command(update, ctx)
    check("/reset_shown refuses a non-admin", "только администратору" in str(update.message.reply_text.call_args_list))

    # Admin clears only their own history.
    update = make_update(ADMIN_ID)
    await bot.reset_shown_command(update, ctx)
    reply_text = str(update.message.reply_text.call_args_list)
    check("/reset_shown confirms clearing for the admin", "очищена" in reply_text, reply_text)
    check("/reset_shown never mentions the shared pool or analytics as affected",
          "статистика не затронуты" in reply_text or "не затронуты" in reply_text, reply_text)

    verify_cache = bot._open_football_cache(now_dt)
    remaining = verify_cache.get_shown_keys(local_date_str(now_dt), ADMIN_ID)
    verify_cache.close()
    check("/reset_shown actually clears the admin's shown_picks rows", remaining == set(), remaining)


def main():
    run()
    asyncio.run(bot_admin_tests())

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
