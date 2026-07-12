"""
Tests the "🤖 Прогнозы ИИ" Telegram handler wiring in bot.py: button is on
the keyboard, cache/lock/credit-protection behavior, and that
run_ai_predictions is invoked through asyncio.to_thread (never blocking
the event loop). run_ai_predictions itself is monkeypatched -- no real
network calls happen here.
"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, ".")
import bot
from ai_predictions.pipeline import PipelineResult

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


def fake_result(text="🤖 Прогноз готов"):
    return PipelineResult(
        report_text=text, events_considered=1, events_excluded_by_window=0,
        candidates_considered=3, saved_count=1, duplicate_count=0, errors=[],
    )


async def main():
    ctx = MagicMock()

    # -- keyboard --------------------------------------------------------
    keyboard = bot.main_keyboard()
    all_callback_data = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    check("AI predictions button is on the main keyboard", bot.AI_PREDICTIONS_PREFIX in all_callback_data, all_callback_data)

    # -- missing keys guard ------------------------------------------------
    bot.ODDS_API_KEY, saved_odds = None, bot.ODDS_API_KEY
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    check("missing ODDS_API_KEY blocks the request with a clear message", query.message.reply_text.await_count == 1)
    bot.ODDS_API_KEY = saved_odds
    bot.FOOTBALL_API_KEY = "fake-football-key"

    # -- happy path / caches the result -------------------------------------
    bot.ai_predictions_cache = None
    bot.last_known_credits = None
    call_count = {"n": 0}

    async def fake_run(*args, **kwargs):
        call_count["n"] += 1
        return fake_result()

    original_run = bot.run_ai_predictions
    bot.run_ai_predictions = lambda *a, **kw: fake_result()  # sync stand-in for asyncio.to_thread
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    check("first request calls run_ai_predictions and replies with the report",
          any("Прогноз готов" in str(c) for c in query.message.reply_text.call_args_list), query.message.reply_text.call_args_list)
    check("result is cached", bot.ai_predictions_cache is not None)

    # -- second request within TTL uses cache, does not call run_ai_predictions again --
    calls_before = query.message.reply_text.await_count
    bot.run_ai_predictions = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not refetch from cache"))
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    check("cached response served without recomputation", query.message.reply_text.await_count == 1)

    # -- credit protection ---------------------------------------------------
    bot.ai_predictions_cache = None
    bot.last_known_credits = bot.MIN_CREDITS_FOR_AI_PREDICTIONS - 1
    bot.run_ai_predictions = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not call API when credits are low"))
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    check("low credits block the AI predictions request",
          any("кредит" in str(c).lower() for c in query.message.reply_text.call_args_list), query.message.reply_text.call_args_list)

    bot.run_ai_predictions = original_run

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
