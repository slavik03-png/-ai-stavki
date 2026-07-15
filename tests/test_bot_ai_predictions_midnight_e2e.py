"""
End-to-end proof at the bot.py button-handler level: pressing
'🤖 Прогнозы ИИ' the evening before, then again the next Yekaterinburg
morning, must trigger a fresh run_football_predictions() call and must
never show the previous day's messages -- reproducing and verifying the
fix for the reported "показал архив за вчерашний день" bug.
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
from ai_predictions.football_pipeline import FootballPipelineResult, select_and_render
from ai_predictions.football_predictions import MarketCandidate
from ai_predictions.prediction_selector import RankedRecommendation

results = []


def fake_pool_entry(fixture_id, now, label):
    """A real pool entry (2026-07-15 pool-based archive redesign) whose
    home team name carries `label` so tests can tell which run's
    candidate is being shown -- the archive re-renders from the pool on
    every request rather than replaying a fixed string, so a plain
    telegram_messages list is not enough to distinguish runs any more."""
    fixture = Fixture(
        fixture_id=fixture_id, kickoff_utc=now + datetime.timedelta(hours=3),
        home_team=label, away_team="Соперник", home_team_id=1, away_team_id=2,
        league_name="Test League", league_country="World", status_short="NS",
    )
    candidate = MarketCandidate(
        fixture=fixture, market_key="h2h_home", market_label_ru="Победа хозяев",
        probability=0.65, completeness=1.0, sample_size_category="full",
        rationale="test", source="recent_form",
    )
    rec = RankedRecommendation(candidate=candidate, signal_level="HIGH")
    return rec, 1.85, "TestBookmaker"


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def make_callback_update():
    update = MagicMock()
    query = MagicMock()
    query.data = bot.AI_PREDICTIONS_PREFIX
    query.answer = AsyncMock()
    query.message = AsyncMock()
    query.from_user = MagicMock(id=999)
    update.callback_query = query
    return update, query


async def main():
    ctx = MagicMock()
    db_path = tempfile.mktemp()

    # 2026-07-14 23:50 Yekaterinburg (UTC+5) = 18:50 UTC
    evening_before = datetime.datetime(2026, 7, 14, 18, 50, tzinfo=datetime.timezone.utc)
    # 2026-07-15 07:00 Yekaterinburg = 02:00 UTC -- new calendar day, only
    # ~7h10m after the evening run, well inside the old 24h TTL.
    morning_after = datetime.datetime(2026, 7, 15, 2, 0, tzinfo=datetime.timezone.utc)

    real_datetime = bot.datetime

    class FrozenDatetime(real_datetime):
        _now = evening_before

        @classmethod
        def now(cls, tz=None):
            return cls._now

    bot.datetime = FrozenDatetime
    bot._open_football_cache = lambda now: FootballCache(db_path=db_path, now=now)
    bot.ai_predictions_cache = None

    calls = {"count": 0}

    def fake_run(*a, **kw):
        calls["count"] += 1
        now = kw.get("now") or (a[1] if len(a) > 1 else evening_before)
        if calls["count"] == 1:
            pool = [fake_pool_entry(1, now, "ЗА ВЧЕРА")]
        else:
            pool = [fake_pool_entry(2, now, "НА СЕГОДНЯ")]
        messages, selected_entries = select_and_render(pool, now, found_fixtures=1, analysed_fixtures=1)
        return FootballPipelineResult(pool=pool, telegram_messages=messages, recommendations_count=len(selected_entries))

    bot.run_football_predictions = fake_run

    # -- evening of 2026-07-14: first press builds and archives ----------
    update, query = make_callback_update()
    await bot.handle_callback(update, ctx)
    evening_text = "\n".join(str(c) for c in query.message.reply_text.call_args_list)
    check("evening press computes and shows yesterday's (14th) result",
          "ЗА ВЧЕРА" in evening_text, evening_text)
    check("exactly one real pipeline run so far", calls["count"] == 1)

    # -- still the evening of the 14th, second press within minutes: must
    # replay the SAME archive, not recompute --------------------------------
    update, query = make_callback_update()
    await bot.handle_callback(update, ctx)
    same_evening_text = "\n".join(str(c) for c in query.message.reply_text.call_args_list)
    check("same-evening second press replays the archive, no recompute",
          "ЗА ВЧЕРА" in same_evening_text and calls["count"] == 1, same_evening_text)

    # -- next Yekaterinburg morning (2026-07-15 07:00 local): even though
    # <24h have elapsed, this MUST trigger a brand-new run and must NOT
    # show the 14th's cached messages ---------------------------------------
    FrozenDatetime._now = morning_after
    update, query = make_callback_update()
    await bot.handle_callback(update, ctx)
    morning_text = "\n".join(str(c) for c in query.message.reply_text.call_args_list)
    check("new calendar day triggers a fresh pipeline run (not just a cache replay)",
          calls["count"] == 2, f"calls={calls['count']}")
    check("new calendar day's message shown", "НА СЕГОДНЯ" in morning_text, morning_text)
    check("previous day's message is NOT shown to the user on the new day",
          "ЗА ВЧЕРА" not in morning_text, morning_text)

    # -- later the same new day: replays the NEW archive, no further recompute --
    update, query = make_callback_update()
    await bot.handle_callback(update, ctx)
    later_text = "\n".join(str(c) for c in query.message.reply_text.call_args_list)
    check("later same (new) day replays the new archive without recomputing",
          "НА СЕГОДНЯ" in later_text and calls["count"] == 2, later_text)

    bot.datetime = real_datetime

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
