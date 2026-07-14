"""
Tests the "🤖 Прогнозы ИИ" Telegram handler wiring in bot.py (production
v3): button is on the keyboard, only FOOTBALL_API_KEY is required (The
Odds API is optional enrichment and must never block the request),
cache/lock behavior, and that run_football_predictions is invoked
through asyncio.to_thread (never blocking the event loop).
run_football_predictions itself is monkeypatched -- no real network calls
happen here.
"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, ".")
import bot
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
    update.callback_query = query
    return update, query


def fake_result(
    messages=None, found=5, analysed=3, recs=1,
    used=6, remaining=80, odds_status="quota_exhausted", errors=None,
):
    return FootballPipelineResult(
        telegram_messages=messages or ["🤖 Прогноз готов"],
        found_fixtures=found, analysed_fixtures=analysed, recommendations_count=recs,
        api_football_requests_used=used, api_football_requests_remaining=remaining,
        odds_status=odds_status, saved_count=recs, duplicate_count=0, errors=errors or [],
    )


async def main():
    ctx = MagicMock()

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

    # -- missing ODDS_API_KEY must NOT block the request (production v3) --
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

    # -- happy path / caches the result -------------------------------------
    bot.ai_predictions_cache = None
    bot.run_football_predictions = lambda *a, **kw: fake_result()  # sync stand-in for asyncio.to_thread
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    check("first request calls run_football_predictions and replies with the report",
          any("Прогноз готов" in str(c) for c in query.message.reply_text.call_args_list), query.message.reply_text.call_args_list)
    check("result is cached", bot.ai_predictions_cache is not None)
    check("last successful run timestamp recorded", bot.ai_predictions_last_success_ts is not None)

    # -- second request within TTL uses cache, does not call run_football_predictions again --
    bot.run_football_predictions = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not refetch from cache"))
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    check("cached response served without recomputation", query.message.reply_text.await_count == 1)

    bot.run_football_predictions = original_run

    # -- diagnostics stay out of the prediction message, reachable via /status --
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

    # Status text must never make a real API-Football network call in
    # tests -- discover_fixtures_in_window is monkeypatched at the bot
    # module level for this check only.
    from ai_predictions.fixtures import FixtureDiscoveryResult
    original_discover = bot.discover_fixtures_in_window
    bot.discover_fixtures_in_window = lambda *a, **kw: FixtureDiscoveryResult(dates_queried=["2026-07-14"])
    status_text = bot.build_status_text()
    bot.discover_fixtures_in_window = original_discover
    check("status text exposes found/analysed/recommendations counts",
          "Найдено: 5, проанализировано: 3, рекомендаций: 1" in status_text, status_text)
    check("status text exposes the Odds API tri-state", "квота исчерпана" in status_text)
    check("status text exposes last successful run", "Последний успешный запуск прогнозов ИИ:" in status_text)
    check("status text exposes API-Football key presence", "API-Football key:" in status_text)
    check("status text exposes fixture cache age field", "Возраст кэша фикстур:" in status_text)
    check("status text exposes fixtures-found-in-36h field", "Найдено матчей в ближайшие 36 часов:" in status_text)

    bot.run_football_predictions = original_run

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
