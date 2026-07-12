"""
Regression tests for the existing Telegram bot (bot.py). These confirm the
football analytics work has not altered bot behavior in any way.
"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, ".")
import bot

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def make_update():
    update = MagicMock()
    update.message = AsyncMock()
    return update


def make_callback_update(data):
    update = MagicMock()
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = AsyncMock()
    update.callback_query = query
    return update, query


async def main():
    ctx = MagicMock()

    update = make_update()
    await bot.start(update, ctx)
    check("/start still replies with menu", update.message.reply_text.await_count == 1)

    update, query = make_callback_update("status")
    await bot.handle_callback(update, ctx)
    check("status button still works", query.message.reply_text.await_count == 1)

    def fake_fetch_odds(sport_keys):
        return [{"sport": sport_keys[0], "commence_time": "2026-07-11T10:00:00Z", "home_team": "A",
                  "away_team": "B", "bookmaker": "Bet365", "market": "h2h", "outcome": "A", "price": 1.5, "point": ""}], "99"
    bot.fetch_odds = fake_fetch_odds

    for kind in ("football", "tennis", "hockey"):
        update, query = make_callback_update(f"odds:{kind}")
        await bot.handle_callback(update, ctx)
        check(f"{kind} button still works", query.message.reply_document.await_count == 1)

    bot.last_known_credits = 99
    update, query = make_callback_update("odds:all")
    await bot.handle_callback(update, ctx)
    check("all sports button still works", query.message.reply_document.await_count == 1)

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
