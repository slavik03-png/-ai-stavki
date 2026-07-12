import asyncio
import os
import csv
import tempfile
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# Only the orchestration package is imported here -- never tracking/ or
# selection_engine/ directly (see tests/test_selection_isolation.py and
# tests/test_tracking_bot_isolation.py, which assert this boundary).
# The bot currently uses the cross-bookmaker value-detection strategy
# (ai_predictions/value_pipeline.py), which only needs real odds -- no
# football statistics provider. ai_predictions/pipeline.py (statistics +
# odds) stays available for when a paid API-Football plan unlocks
# current-season data.
from ai_predictions.value_pipeline import run_value_predictions

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

AI_PREDICTIONS_PREFIX = "ai_predictions"
MIN_CREDITS_FOR_AI_PREDICTIONS = 10
CACHE_LABELS_AI_PREDICTIONS = "🤖 Прогнозы ИИ"

SPORTS = {
    "football": [
        "soccer_epl",
        "soccer_spain_la_liga",
        "soccer_italy_serie_a",
        "soccer_germany_bundesliga",
        "soccer_france_ligue_one",
        "soccer_uefa_champs_league",
        "soccer_uefa_europa_league",
    ],
    "tennis": [
        "tennis_atp",
        "tennis_wta",
    ],
    "hockey": [
        "icehockey_nhl",
        "icehockey_sweden_hockey_league",
        "icehockey_switzerland_national_league",
    ],
}

REGIONS = "eu"
MARKETS = "h2h,spreads,totals"
ODDS_FORMAT = "decimal"

CACHE_TTL_SECONDS = 30 * 60
MIN_CREDITS_FOR_ALL = 30

CACHE_LABELS = {
    "football": "⚽ Футбол",
    "tennis": "🎾 Теннис",
    "hockey": "🏒 Хоккей",
    "all": "🎯 Вся линия",
}

# cache[prefix] = {"csv_path": str, "message": str, "timestamp": float}
cache: Dict[str, Dict[str, Any]] = {}
cache_locks: Dict[str, asyncio.Lock] = {prefix: asyncio.Lock() for prefix in CACHE_LABELS}
last_known_credits: Optional[int] = None

# ai_predictions_cache = {"message": str, "timestamp": float} -- separate
# from the odds cache above (different prefix namespace, different lock).
ai_predictions_cache: Optional[Dict[str, Any]] = None
ai_predictions_lock = asyncio.Lock()


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Получить всю линию", callback_data="odds:all")],
        [
            InlineKeyboardButton("⚽ Футбол", callback_data="odds:football"),
            InlineKeyboardButton("🎾 Теннис", callback_data="odds:tennis"),
            InlineKeyboardButton("🏒 Хоккей", callback_data="odds:hockey"),
        ],
        [InlineKeyboardButton("🤖 Прогнозы ИИ", callback_data=AI_PREDICTIONS_PREFIX)],
        [InlineKeyboardButton("ℹ️ Статус", callback_data="status")],
    ])


def fetch_odds(sport_keys: List[str]) -> tuple[list[dict[str, Any]], str]:
    rows: list[dict[str, Any]] = []
    credits_left = "неизвестно"

    for sport in sport_keys:
        url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": REGIONS,
            "markets": MARKETS,
            "oddsFormat": ODDS_FORMAT,
            "dateFormat": "iso",
        }
        response = requests.get(url, params=params, timeout=30)
        credits_left = response.headers.get("x-requests-remaining", credits_left)

        if response.status_code != 200:
            rows.append({
                "sport": sport,
                "commence_time": "ERROR",
                "home_team": f"HTTP {response.status_code}",
                "away_team": response.text[:200],
                "bookmaker": "",
                "market": "",
                "outcome": "",
                "price": "",
                "point": "",
            })
            continue

        events = response.json()
        for event in events:
            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    for outcome in market.get("outcomes", []):
                        rows.append({
                            "sport": sport,
                            "commence_time": event.get("commence_time", ""),
                            "home_team": event.get("home_team", ""),
                            "away_team": event.get("away_team", ""),
                            "bookmaker": bookmaker.get("title", ""),
                            "market": market.get("key", ""),
                            "outcome": outcome.get("name", ""),
                            "price": outcome.get("price", ""),
                            "point": outcome.get("point", ""),
                        })
    return rows, credits_left


def save_csv(rows: list[dict[str, Any]], prefix: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
    path = os.path.join(tempfile.gettempdir(), f"ai_stavki_{prefix}_{now}.csv")
    fieldnames = ["sport", "commence_time", "home_team", "away_team", "bookmaker", "market", "outcome", "price", "point"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def parse_credits(value: Optional[str]) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cache_status_lines() -> str:
    now = datetime.now(timezone.utc).timestamp()
    lines = []
    for prefix, label in CACHE_LABELS.items():
        entry = cache.get(prefix)
        if not entry:
            lines.append(f"{label}: нет данных")
            continue
        remaining = CACHE_TTL_SECONDS - (now - entry["timestamp"])
        if remaining <= 0:
            cache.pop(prefix, None)
            lines.append(f"{label}: нет данных")
        else:
            minutes = max(1, int(remaining // 60) + (1 if remaining % 60 else 0))
            lines.append(f"{label}: есть (обновится через {minutes} мин)")
    return "\n".join(lines)


def summarize(rows: list[dict[str, Any]], credits_left: str) -> str:
    events = set()
    markets = set()
    sports = set()
    for r in rows:
        if r.get("commence_time") != "ERROR":
            events.add((r.get("sport"), r.get("commence_time"), r.get("home_team"), r.get("away_team")))
            markets.add(r.get("market"))
            sports.add(r.get("sport"))
    return (
        "🎯 AI Ставки\n\n"
        f"Получено строк: {len(rows)}\n"
        f"Событий: {len(events)}\n"
        f"Видов спорта: {len(sports)}\n"
        f"Рынки: {', '.join(sorted(m for m in markets if m)) or 'нет'}\n"
        f"Осталось кредитов API: {credits_left}\n\n"
        "CSV-файл прикрепляю ниже. Его можно переслать в ChatGPT для анализа."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🎯 *AI Ставки*\n\n"
        "Личный бот для получения линии и коэффициентов.\n\n"
        "Нажми кнопку ниже:"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())


async def send_cached(query, prefix: str) -> None:
    entry = cache[prefix]
    await query.message.reply_text(
        "🗄 Данные из кэша (не старше 30 минут), новый запрос к API не выполнялся.\n\n"
        + entry["message"],
        reply_markup=main_keyboard(),
    )
    with open(entry["csv_path"], "rb") as f:
        await query.message.reply_document(document=f, filename=os.path.basename(entry["csv_path"]))


async def handle_ai_predictions(query) -> None:
    global ai_predictions_cache

    if not ODDS_API_KEY:
        await query.message.reply_text(
            "❌ Для прогнозов ИИ нужен ключ ODDS_API_KEY.",
            reply_markup=main_keyboard(),
        )
        return

    now_ts = datetime.now(timezone.utc).timestamp()
    if ai_predictions_cache and (now_ts - ai_predictions_cache["timestamp"]) < CACHE_TTL_SECONDS:
        await query.message.reply_text(
            "🗄 Прогноз из кэша (не старше 30 минут), новый запрос не выполнялся.\n\n"
            + ai_predictions_cache["message"],
            reply_markup=main_keyboard(),
        )
        return

    if ai_predictions_lock.locked():
        await query.message.reply_text(
            "⏳ Прогноз уже формируется. Подожди немного и нажми кнопку снова.",
            reply_markup=main_keyboard(),
        )
        return

    async with ai_predictions_lock:
        now_ts = datetime.now(timezone.utc).timestamp()
        if ai_predictions_cache and (now_ts - ai_predictions_cache["timestamp"]) < CACHE_TTL_SECONDS:
            await query.message.reply_text(
                "🗄 Прогноз из кэша (не старше 30 минут), новый запрос не выполнялся.\n\n"
                + ai_predictions_cache["message"],
                reply_markup=main_keyboard(),
            )
            return

        if last_known_credits is not None and last_known_credits < MIN_CREDITS_FOR_AI_PREDICTIONS:
            await query.message.reply_text(
                "⚠️ Слишком мало кредитов The Odds API "
                f"(осталось: {last_known_credits}). Запрос прогнозов ИИ отменён, "
                "чтобы не исчерпать лимит.",
                reply_markup=main_keyboard(),
            )
            return

        await query.message.reply_text(
            "🤖 Анализирую матчи ближайших 36 часов (реальные коэффициенты нескольких букмекеров)... "
            "Это может занять минуту."
        )
        try:
            result = await asyncio.to_thread(run_value_predictions)
            ai_predictions_cache = {
                "message": result.report_text,
                "timestamp": datetime.now(timezone.utc).timestamp(),
            }
            await query.message.reply_text(result.report_text, reply_markup=main_keyboard())
            if result.errors:
                error_preview = "\n".join(result.errors[:5])
                await query.message.reply_text(f"⚠️ Часть данных получить не удалось:\n{error_preview}")
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка при формировании прогнозов ИИ: {e}", reply_markup=main_keyboard())


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global last_known_credits
    query = update.callback_query
    await query.answer()

    if query.data == "status":
        ok_bot = "✅" if TELEGRAM_BOT_TOKEN else "❌"
        ok_odds = "✅" if ODDS_API_KEY else "❌"
        ok_football = "✅" if FOOTBALL_API_KEY else "❌"
        credits_text = str(last_known_credits) if last_known_credits is not None else "неизвестно (ещё не было запросов)"
        ai_predictions_status = "нет данных" if not ai_predictions_cache else "есть (обновится в течение 30 минут)"
        text = (
            "ℹ️ Статус AI Ставки\n\n"
            f"Telegram token: {ok_bot}\n"
            f"The Odds API key: {ok_odds}\n"
            f"API-Football key: {ok_football}\n"
            f"Осталось кредитов The Odds API: {credits_text}\n\n"
            "Кэш (хранится 30 минут):\n"
            f"{cache_status_lines()}\n"
            f"{CACHE_LABELS_AI_PREDICTIONS}: {ai_predictions_status}"
        )
        await query.message.reply_text(text, reply_markup=main_keyboard())
        return

    if query.data == AI_PREDICTIONS_PREFIX:
        await handle_ai_predictions(query)
        return

    if query.data and query.data.startswith("odds:"):
        kind = query.data.split(":", 1)[1]
        if kind == "all":
            sport_keys = SPORTS["football"] + SPORTS["tennis"] + SPORTS["hockey"]
        else:
            sport_keys = SPORTS.get(kind, [])
        prefix = kind

        if not ODDS_API_KEY:
            await query.message.reply_text("❌ Не найден ODDS_API_KEY в настройках Render.")
            return

        now_ts = datetime.now(timezone.utc).timestamp()
        entry = cache.get(prefix)
        if entry and (now_ts - entry["timestamp"]) < CACHE_TTL_SECONDS:
            await send_cached(query, prefix)
            return

        lock = cache_locks[prefix]
        if lock.locked():
            await query.message.reply_text(
                f"⏳ Запрос для «{CACHE_LABELS[prefix]}» уже выполняется. "
                "Подожди немного и нажми кнопку снова.",
                reply_markup=main_keyboard(),
            )
            return

        async with lock:
            # Re-check cache in case it was filled while we were waiting for the lock.
            now_ts = datetime.now(timezone.utc).timestamp()
            entry = cache.get(prefix)
            if entry and (now_ts - entry["timestamp"]) < CACHE_TTL_SECONDS:
                await send_cached(query, prefix)
                return

            if prefix == "all" and last_known_credits is not None and last_known_credits < MIN_CREDITS_FOR_ALL:
                await query.message.reply_text(
                    "⚠️ Слишком мало кредитов The Odds API "
                    f"(осталось: {last_known_credits}). Запрос всей линии отменён, "
                    "чтобы не исчерпать лимит. Попробуй запросить один вид спорта "
                    "или подожди обновления лимита.",
                    reply_markup=main_keyboard(),
                )
                return

            await query.message.reply_text("⏳ Получаю линию... Подожди 10–30 секунд.")
            try:
                rows, credits_left = await asyncio.to_thread(fetch_odds, sport_keys)
                parsed_credits = parse_credits(credits_left)
                if parsed_credits is not None:
                    last_known_credits = parsed_credits
                csv_path = save_csv(rows, prefix)
                message = summarize(rows, credits_left)
                cache[prefix] = {
                    "csv_path": csv_path,
                    "message": message,
                    "timestamp": datetime.now(timezone.utc).timestamp(),
                }
                await query.message.reply_text(message, reply_markup=main_keyboard())
                with open(csv_path, "rb") as f:
                    await query.message.reply_document(document=f, filename=os.path.basename(csv_path))
            except Exception as e:
                await query.message.reply_text(f"❌ Ошибка: {e}", reply_markup=main_keyboard())


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Нет переменной TELEGRAM_BOT_TOKEN")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    print("AI Ставки Bot запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
