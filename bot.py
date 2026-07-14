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
#
# Production v3 (2026-07-14): the bot's "🤖 Прогнозы ИИ" button runs the
# API-Football-only pipeline (ai_predictions/football_pipeline.py).
# API-Football is the primary and sufficient data source for
# recommendations; The Odds API is purely optional coefficient enrichment
# and can never block or reduce recommendations (see that module's
# docstring). The older odds-driven fixture-discovery-first pipeline
# (ai_predictions/value_pipeline.py) stays available/tested but is no
# longer wired into the bot, since it produces zero candidates whenever
# The Odds API quota is exhausted -- exactly the failure this version
# fixes.
from ai_predictions.football_cache import FootballCache
from ai_predictions.football_pipeline import run_football_predictions
from ai_predictions.fixtures import discover_fixtures_in_window
from ai_predictions.value_config import FIXTURE_LIST_CACHE_TTL_HOURS

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

# ai_predictions_cache = {"messages": List[str], "timestamp": float} --
# separate from the odds cache above (different prefix namespace,
# different lock). Holds only the concise, user-facing signal messages.
ai_predictions_cache: Optional[Dict[str, Any]] = None
ai_predictions_lock = asyncio.Lock()

# Latest run's technical diagnostics, shown only via the "ℹ️ Статус"
# button / /status command -- never sent as part of the normal
# prediction message. Kept separately so /status can report the last
# real run even after the 30-minute prediction cache itself expires.
ai_predictions_last_diagnostics: Optional[Dict[str, Any]] = None

# UTC timestamp (float) of the last successful "🤖 Прогнозы ИИ" run, for
# the required /status "last successful prediction run" field. None until
# the first successful run since the process started.
ai_predictions_last_success_ts: Optional[float] = None


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


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status -- same technical diagnostics as the 'ℹ️ Статус' button."""
    await update.message.reply_text(build_status_text(), reply_markup=main_keyboard())


async def send_cached(query, prefix: str) -> None:
    entry = cache[prefix]
    await query.message.reply_text(
        "🗄 Данные из кэша (не старше 30 минут), новый запрос к API не выполнялся.\n\n"
        + entry["message"],
        reply_markup=main_keyboard(),
    )
    with open(entry["csv_path"], "rb") as f:
        await query.message.reply_document(document=f, filename=os.path.basename(entry["csv_path"]))


async def _send_cached_predictions(query) -> None:
    messages = ai_predictions_cache["messages"]
    await query.message.reply_text(
        "🗄 Прогноз из кэша (не старше 30 минут), новый запрос не выполнялся.\n\n"
        + messages[0],
        reply_markup=main_keyboard(),
    )
    for extra in messages[1:]:
        await query.message.reply_text(extra, reply_markup=main_keyboard())


async def handle_ai_predictions(query) -> None:
    global ai_predictions_cache, ai_predictions_last_diagnostics

    # API-Football is the only REQUIRED key -- The Odds API is optional
    # coefficient enrichment only (production v3).
    if not FOOTBALL_API_KEY:
        await query.message.reply_text(
            "❌ Для прогнозов ИИ нужен ключ: FOOTBALL_API_KEY.",
            reply_markup=main_keyboard(),
        )
        return

    now_ts = datetime.now(timezone.utc).timestamp()
    if ai_predictions_cache and (now_ts - ai_predictions_cache["timestamp"]) < CACHE_TTL_SECONDS:
        await _send_cached_predictions(query)
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
            await _send_cached_predictions(query)
            return

        # Odds API credits never gate this button any more -- API-Football
        # is sufficient on its own; The Odds API is optional enrichment.
        await query.message.reply_text(
            "🤖 Анализирую матчи ближайших 36 часов по данным API-Football... "
            "Это может занять минуту."
        )
        try:
            global ai_predictions_last_success_ts
            result = await asyncio.to_thread(run_football_predictions)
            messages = result.telegram_messages or ["На ближайшие 36 часов подходящих сигналов не найдено."]
            now_ts2 = datetime.now(timezone.utc).timestamp()
            ai_predictions_cache = {
                "messages": messages,
                "timestamp": now_ts2,
            }
            # Full technical diagnostics are kept only for /status -- never
            # sent here.
            ai_predictions_last_diagnostics = {
                "found_fixtures": result.found_fixtures,
                "analysed_fixtures": result.analysed_fixtures,
                "recommendations_count": result.recommendations_count,
                "api_football_requests_used": result.api_football_requests_used,
                "api_football_requests_remaining": result.api_football_requests_remaining,
                "odds_status": result.odds_status,
                "errors": result.errors,
                "timestamp": now_ts2,
            }
            ai_predictions_last_success_ts = now_ts2
            for message in messages:
                await query.message.reply_text(message, reply_markup=main_keyboard())
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка при формировании прогнозов ИИ: {e}", reply_markup=main_keyboard())


def _format_ago(now_dt: datetime, then_dt: datetime) -> str:
    delta = now_dt - then_dt
    hours = delta.total_seconds() / 3600.0
    if hours < 1:
        return f"{int(delta.total_seconds() / 60)} мин назад"
    return f"{hours:.1f} ч назад"


def _odds_api_status_text() -> str:
    """Best-effort, cheap tri-state read for /status -- reuses the last
    known diagnostic status from the most recent run rather than making a
    fresh Odds API call just to render /status."""
    if not ODDS_API_KEY:
        return "недоступен (не задан ключ)"
    if ai_predictions_last_diagnostics:
        status = ai_predictions_last_diagnostics.get("odds_status")
        if status == "quota_exhausted":
            return "квота исчерпана"
        if status == "available":
            return "доступен"
        if status == "unavailable":
            return "недоступен"
    return "неизвестно (ещё не было запросов)"


def build_status_text() -> str:
    """All technical/diagnostic information lives here -- the normal
    prediction message never shows any of this. Fields match the
    production-fix spec's required /status list exactly: Telegram token,
    API-Football key, API-Football requests remaining, fixture cache age,
    fixtures found in the next 36h, Odds API tri-state, last successful
    run."""
    ok_bot = "доступен" if TELEGRAM_BOT_TOKEN else "отсутствует"
    ok_football = "доступен" if FOOTBALL_API_KEY else "отсутствует"

    now_dt = datetime.now(timezone.utc)
    requests_remaining_text = "неизвестно"
    fixture_cache_age_text = "нет данных (ещё не запрашивались)"
    fixtures_found_text = "неизвестно (ещё не было запросов)"

    if FOOTBALL_API_KEY:
        try:
            football_cache = FootballCache(now=now_dt)
            requests_remaining_text = str(football_cache.requests_available())
            discovery = discover_fixtures_in_window(FOOTBALL_API_KEY, football_cache, now_dt)
            fixtures_found_text = str(len(discovery.fixtures))
            newest_cached_at = None
            for date_str in discovery.dates_queried:
                cached_at = football_cache.cached_at(f"fixtures:date:{date_str}")
                if cached_at is not None and (newest_cached_at is None or cached_at > newest_cached_at):
                    newest_cached_at = cached_at
            if newest_cached_at is not None:
                fixture_cache_age_text = _format_ago(now_dt, newest_cached_at)
            football_cache.close()
        except Exception:
            pass

    last_run_text = (
        _format_ago(now_dt, datetime.fromtimestamp(ai_predictions_last_success_ts, tz=timezone.utc))
        if ai_predictions_last_success_ts is not None
        else "ещё не было успешных запусков"
    )

    lines = [
        "ℹ️ Статус AI Ставки",
        "",
        f"Telegram token: {ok_bot}",
        f"API-Football key: {ok_football}",
        f"Осталось запросов к API-Football сегодня: {requests_remaining_text}",
        f"Возраст кэша фикстур: {fixture_cache_age_text}",
        f"Найдено матчей в ближайшие 36 часов: {fixtures_found_text}",
        f"The Odds API: {_odds_api_status_text()}",
        f"Последний успешный запуск прогнозов ИИ: {last_run_text}",
        "",
        "Кэш линии (хранится 30 минут):",
        cache_status_lines(),
    ]

    if ai_predictions_last_diagnostics:
        lines.append("")
        lines.append("Прогнозы ИИ — последний запуск:")
        lines.append(
            f"Найдено: {ai_predictions_last_diagnostics.get('found_fixtures', 0)}, "
            f"проанализировано: {ai_predictions_last_diagnostics.get('analysed_fixtures', 0)}, "
            f"рекомендаций: {ai_predictions_last_diagnostics.get('recommendations_count', 0)}"
        )
        run_errors = ai_predictions_last_diagnostics.get("errors")
        if run_errors:
            lines.append(f"Примечания: {'; '.join(run_errors[:3])}")

    return "\n".join(lines)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global last_known_credits
    query = update.callback_query
    await query.answer()

    if query.data == "status":
        await query.message.reply_text(build_status_text(), reply_markup=main_keyboard())
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
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    print("AI Ставки Bot запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
