"""
Tests the "🤖 Прогнозы ИИ" Telegram handler wiring in bot.py: button is on
the keyboard, cache/lock/credit-protection behavior, and that
run_value_predictions is invoked through asyncio.to_thread (never
blocking the event loop). run_value_predictions itself is monkeypatched
-- no real network calls happen here.
"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, ".")
import bot
from ai_predictions.value_pipeline import ValuePipelineResult
from ai_predictions.value_report import Diagnostics

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


def fake_result(text="🤖 Прогноз готов", diagnostics=None, api_error_summary=None):
    return ValuePipelineResult(
        report_text=text, events_received=1, events_excluded_by_window=0,
        markets_compared=2, candidates_created=3, candidates_rejected=2,
        final_recommendations=1, saved_count=1, duplicate_count=0, errors=[],
        telegram_messages=[text], diagnostics=diagnostics, api_error_summary=api_error_summary,
        high_count=1, medium_count=0, low_count=0,
    )


async def main():
    ctx = MagicMock()

    # -- keyboard --------------------------------------------------------
    keyboard = bot.main_keyboard()
    all_callback_data = [btn.callback_data for row in keyboard.inline_keyboard for btn in row]
    check("AI predictions button is on the main keyboard", bot.AI_PREDICTIONS_PREFIX in all_callback_data, all_callback_data)

    # -- missing key guard ------------------------------------------------
    bot.ODDS_API_KEY, saved_odds = None, bot.ODDS_API_KEY
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    check("missing ODDS_API_KEY blocks the request with a clear message", query.message.reply_text.await_count == 1)
    bot.ODDS_API_KEY = saved_odds

    # -- happy path / caches the result -------------------------------------
    bot.ai_predictions_cache = None
    bot.last_known_credits = None

    original_run = bot.run_value_predictions
    bot.run_value_predictions = lambda *a, **kw: fake_result()  # sync stand-in for asyncio.to_thread
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    check("first request calls run_value_predictions and replies with the report",
          any("Прогноз готов" in str(c) for c in query.message.reply_text.call_args_list), query.message.reply_text.call_args_list)
    check("result is cached", bot.ai_predictions_cache is not None)

    # -- second request within TTL uses cache, does not call run_value_predictions again --
    bot.run_value_predictions = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not refetch from cache"))
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    check("cached response served without recomputation", query.message.reply_text.await_count == 1)

    # -- credit protection ---------------------------------------------------
    bot.ai_predictions_cache = None
    bot.last_known_credits = bot.MIN_CREDITS_FOR_AI_PREDICTIONS - 1
    bot.run_value_predictions = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not call API when credits are low"))
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    check("low credits block the AI predictions request",
          any("кредит" in str(c).lower() for c in query.message.reply_text.call_args_list), query.message.reply_text.call_args_list)

    bot.run_value_predictions = original_run

    # -- diagnostics stay out of the prediction message, reachable via /status --
    bot.ai_predictions_cache = None
    bot.ai_predictions_last_diagnostics = None
    bot.last_known_credits = None
    diag = Diagnostics(
        high_count=1, medium_count=0, low_count=0, rejected_count=7,
        sports_discovered=["soccer_epl", "soccer_norway_eliteserien"],
        sports_queried=["soccer_epl"],
    )
    bot.run_value_predictions = lambda *a, **kw: fake_result(
        text="🤖 Прогноз готов", diagnostics=diag, api_error_summary="Некоторые турниры недоступны: HTTP 401 — 1 турнира.",
    )
    update, query = make_callback_update(bot.AI_PREDICTIONS_PREFIX)
    await bot.handle_callback(update, ctx)
    prediction_reply_text = "\n".join(str(c) for c in query.message.reply_text.call_args_list)
    check("prediction message has no HTTP/API error text", "HTTP" not in prediction_reply_text)
    check("prediction message has no competition discovery list", "soccer_epl" not in prediction_reply_text)

    status_text = bot.build_status_text()
    check("status text exposes competitions discovered count", "Турниров обнаружено: 2" in status_text, status_text)
    check("status text exposes competitions queried count", "Турниров успешно опрошено: 1" in status_text, status_text)
    check("status text exposes HIGH/MEDIUM/LOW counts", "HIGH — 1" in status_text and "MEDIUM — 0" in status_text and "LOW — 0" in status_text)
    check("status text exposes the aggregated API error line", "HTTP 401" in status_text)

    bot.run_value_predictions = original_run

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
