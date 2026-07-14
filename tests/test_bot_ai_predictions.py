"""
Tests the "🤖 Прогнозы ИИ" Telegram handler wiring in bot.py, including the
strict daily archive (2026-07-15 fix): the first request of the day runs
run_football_predictions and persists the result; every later request
within 24h replays the saved archive and never recomputes/refetches.
Only FOOTBALL_API_KEY is required (The Odds API is optional enrichment and
must never block the request).

run_football_predictions is monkeypatched -- no real network calls happen
here. The persistent football_cache/archive store is also monkeypatched to
an isolated tempfile database (via bot._open_football_cache) so this file
never reads or writes the real production cache.
"""

import asyncio
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, ".")
import bot
from ai_predictions.football_cache import FootballCache
from ai_predictions.football_pipeline import FootballPipelineResult

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def make_callback_update(data):
    update = MagicMock()
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = AsyncMock()
    query.from_user = MagicMock(id=999)
    update.callback_query = query
    return update, query


def make_update():
    update = MagicMock()
    update.message = AsyncMock()
    update.effective_user = MagicMock(id=999)
    return update


def fake_result(
    messages=None, found=5, analysed=3, recs=1,
    used=6, remaining=80, used_today=20, odds_status="quota_exhausted", errors=None,
):
    return FootballPipelineResult(
        telegram_messages=messages or ["🤖 Прогноз готов"],
        found_fixtures=found, analysed_fixtures=analysed, fully_stat_fixtures=analysed, recommendations_count=recs,
        api_football_requests_used=used, api_football_requests_remaining=remaining,
        api_football_requests_used_today=used_today,
        odds_status=odds_status, saved_count=recs, duplicate_count=0, errors=errors or [],
    )


async def main():
    ctx = MagicMock()

    # Every test gets its own fresh, isolated on-disk cache/archive so
    # runs never see a previous test's (or real production's) archive.
    def fresh_cache_factory():
        path = tempfile.mktemp()
        return lambda now: FootballCache(db_path=path, now=now)

    bot._open_football_cache = fresh_cache_factory()

    # -- keyboard --------------------------------------------------------
    keyboard = bot.main_keyboard()
    all_callback_data = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    check("AI predictions button is on the main keyboard", bot.AI_PREDICTIONS_PREFIX in all_callback_data, all_callback_data)

    # -- missing FOOTBALL_API_KEY guard -----------------------------------
    bot.FOOTBALL_API_KEY, saved_football = None, bot.FOOTBALL_API_KEY
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    check("missing FOOTBALL_API_KEY blocks the request with a clear message", query.message.reply_text.await_count == 1)
    bot.FOOTBALL_API_KEY = saved_football

    # -- missing ODDS_API_KEY must NOT block the request -------------------
    bot._open_football_cache = fresh_cache_factory()
    bot.ODDS_API_KEY, saved_odds = None, bot.ODDS_API_KEY
    bot.ai_predictions_cache = None
    original_run = bot.run_football_predictions
    bot.run_football_predictions = lambda *a, **kw: fake_result(odds_status="unavailable")
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    check(
        "missing ODDS_API_KEY does not block the request",
        any("Прогноз готов" in str(c) for c in query.message.reply_text.call_args_list),
        query.message.reply_text.call_args_list,
    )
    bot.ODDS_API_KEY = saved_odds

    # -- happy path / first request of the day computes and archives ------
    bot._open_football_cache = fresh_cache_factory()
    bot.ai_predictions_cache = None
    bot.run_football_predictions = lambda *a, **kw: fake_result()
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    check("first request calls run_football_predictions and replies with the report",
          any("Прогноз готов" in str(c) for c in query.message.reply_text.call_args_list), query.message.reply_text.call_args_list)
    check("result is cached in-process", bot.ai_predictions_cache is not None)
    check("last successful run timestamp recorded", bot.ai_predictions_last_success_ts is not None)

    # -- second request within 24h uses the persisted archive, never
    # recomputes and never touches run_football_predictions again, even
    # from a "different process" (fresh _open_football_cache pointing at
    # the SAME db file simulates that). --------------------------------
    same_db_factory_path = tempfile.mktemp()
    bot._open_football_cache = lambda now: FootballCache(db_path=same_db_factory_path, now=now)
    bot.run_football_predictions = lambda *a, **kw: fake_result(messages=["🤖 Первый прогон"])
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    check("archive is populated on first press against this db",
          any("Первый прогон" in str(c) for c in query.message.reply_text.call_args_list))

    bot.run_football_predictions = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not recompute from the persisted archive"))
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    archive_reply_text = "\n".join(str(c) for c in query.message.reply_text.call_args_list)
    check("archived response served without recomputation", "Первый прогон" in archive_reply_text, archive_reply_text)
    check("archived response is labelled as coming from the daily archive", "суточного архива" in archive_reply_text)

    bot.run_football_predictions = original_run

    # -- diagnostics stay out of the prediction message, reachable via /status --
    bot._open_football_cache = fresh_cache_factory()
    bot.ai_predictions_cache = None
    bot.ai_predictions_last_diagnostics = None
    bot.run_football_predictions = lambda *a, **kw: fake_result(
        messages=["🤖 Прогноз готов"], found=5, analysed=3, recs=1,
        used=6, remaining=80, odds_status="quota_exhausted",
        errors=["ODDS_API_QUOTA_EXHAUSTED: квота The Odds API исчерпана"],
    )
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    prediction_reply_text = "\n".join(str(c) for c in query.message.reply_text.call_args_list)
    check("prediction message has no HTTP/API error text", "HTTP" not in prediction_reply_text)
    check("prediction message has no quota diagnostics", "QUOTA" not in prediction_reply_text)

    # -- /status never triggers a live API-Football call, only reads the
    # persisted archive + quota counters ---------------------------------
    status_text = bot.build_status_text()
    check("status text exposes saved match/recommendation counts",
          "Сохранено матчей: 5" in status_text and "Сохранено рекомендаций: 1" in status_text, status_text)
    check("status text exposes the Odds API tri-state", "квота исчерпана" in status_text)
    check("status text exposes last successful archive update", "Последнее успешное обновление:" in status_text)
    check("status text exposes API-Football key presence", "API-Football key:" in status_text)
    check("status text exposes archive age field", "Возраст архива:" in status_text)
    check("status text exposes requests-used-today field", "Использовано запросов к API-Football сегодня:" in status_text)
    check("status text never mentions live fixture discovery fields", "Найдено матчей в ближайшие 36 часов" not in status_text)

    # -- admin-only /refresh_data ------------------------------------------
    bot.ADMIN_TELEGRAM_IDS = {999}
    update = make_update()
    await bot.refresh_data_command(update, ctx)
    check("admin sees the confirmation prompt", "Подтвердить обновление" in str(update.message.reply_text.call_args_list))

    update.effective_user = MagicMock(id=1)
    update2 = make_update()
    update2.effective_user = MagicMock(id=1)
    await bot.refresh_data_command(update2, ctx)
    check("non-admin is refused", "только администратору" in str(update2.message.reply_text.call_args_list))

    bot._open_football_cache = fresh_cache_factory()
    bot.run_football_predictions = lambda *a, **kw: fake_result(messages=["🤖 Принудительно обновлено"])
    update, query = make_callback_update(bot.ADMIN_REFRESH_CONFIRM_PREFIX)  # admin (id=999)
    await bot.handle_callback(update, ctx)
    forced_reply_text = "\n".join(str(c) for c in query.message.reply_text.call_args_list)
    check("admin-confirmed refresh runs the pipeline even if not requested by the normal button",
          "Принудительно обновлено" in forced_reply_text, forced_reply_text)

    update, query = make_callback_update(bot.ADMIN_REFRESH_CONFIRM_PREFIX)
    query.from_user = MagicMock(id=1)
    await bot.handle_callback(update, ctx)
    check("non-admin callback confirmation is refused", "Недостаточно прав" in str(query.message.reply_text.call_args_list))

    bot.run_football_predictions = original_run

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
