import asyncio
import logging
import os
import csv
import tempfile
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

import requests

# INFO-level logging is required so the daily-archive freshness decisions
# logged by ai_predictions/football_pipeline.py (which calendar day the
# archive vs "now" fall on, and why it was accepted/rejected/rebuilt) are
# actually visible in the workflow console -- the root logger defaults to
# WARNING otherwise and would silently swallow them.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
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
from ai_predictions.football_pipeline import (
    DailyArchive,
    is_refresh_in_progress,
    load_daily_archive,
    mark_refresh_in_progress,
    reselect_from_archive,
    run_football_predictions,
    save_daily_archive,
)
from ai_predictions.prediction_report import render_nothing_left_for_user_message
from ai_predictions.value_config import DAILY_ARCHIVE_TTL_HOURS, LIVE_CACHE_TTL_MINUTES, MIN_LEAD_TIME_MINUTES
from ai_predictions.window import format_user_time, local_date_str

# Live in-play predictions mode (2026-07-15, Task #11): its own, fully
# independent pipeline/cache -- never shares a cache key, storage call, or
# code path with the "🤖 Прогнозы ИИ" pipeline above (see
# tests/test_live_pipeline_isolation.py).
from ai_predictions.live_pipeline import cache_age_seconds, load_cached_live_result, run_live_predictions_cached

# The AI Betting Analytics module (analytics/) is a new, independent
# top-level package -- like ai_predictions/, it is fine for bot.py to
# import it directly. It never imports tracking/ or selection_engine/
# back into bot.py's own import graph (see tests/test_tracking_bot_isolation.py
# and tests/test_selection_isolation.py, which only forbid bot.py from
# importing those two packages specifically).
from analytics.config import DEFAULT_STAKE, RESULT_CHECK_INTERVAL_MINUTES
from analytics.export import export_csv, export_excel
from analytics.reports import admin_report, compact_report
from analytics.result_checker import run_check_cycle
from analytics.storage import AnalyticsStorage

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")

#: Telegram numeric user IDs allowed to force a refresh of the strict
#: daily archive via /refresh_data (requirement: an admin-only action,
#: never triggered by the regular "🤖 Прогнозы ИИ"/"ℹ️ Статус" buttons).
#: Comma-separated in the env var; empty/unset means nobody can force a
#: refresh (fails closed, never silently open to everyone).
ADMIN_TELEGRAM_IDS = {
    int(raw) for raw in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if raw.strip().isdigit()
}

AI_PREDICTIONS_PREFIX = "ai_predictions"
ADMIN_REFRESH_CONFIRM_PREFIX = "admin_refresh_confirm"
STATISTICS_PREFIX = "analytics_stats"
LIVE_PREDICTIONS_PREFIX = "live_predictions"
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

# In-process fast-path mirror of the persisted daily archive (see
# ai_predictions/football_pipeline.py's DailyArchive/load_daily_archive/
# save_daily_archive) -- avoids re-opening the SQLite cache for every
# button press within the same process. The SQLite-backed archive is the
# real source of truth (survives restarts, shared across any concurrent
# process); this dict is purely a latency shortcut and is always kept in
# sync with it.
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

# "🔴 Live" mode's own lock + last-diagnostics mirror (2026-07-15, Task
# #11) -- deliberately separate variables from the ai_predictions_* ones
# above so Live's own 10-minute cache/lock can never interact with, block,
# or be confused with the daily pre-match archive/lock.
live_predictions_lock = asyncio.Lock()
live_predictions_last_diagnostics: Optional[Dict[str, Any]] = None


def _open_football_cache(now: datetime) -> FootballCache:
    """Single seam for opening the persistent API-Football cache/archive
    store -- tests monkeypatch this to point at an isolated tempfile
    database instead of the real production one (see
    tests/test_bot_ai_predictions.py)."""
    return FootballCache(now=now)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Получить всю линию", callback_data="odds:all")],
        [
            InlineKeyboardButton("⚽ Футбол", callback_data="odds:football"),
            InlineKeyboardButton("🎾 Теннис", callback_data="odds:tennis"),
            InlineKeyboardButton("🏒 Хоккей", callback_data="odds:hockey"),
        ],
        [InlineKeyboardButton("🤖 Прогнозы ИИ", callback_data=AI_PREDICTIONS_PREFIX)],
        [InlineKeyboardButton("🔴 Live", callback_data=LIVE_PREDICTIONS_PREFIX)],
        [InlineKeyboardButton("📈 Статистика", callback_data=STATISTICS_PREFIX)],
        [InlineKeyboardButton("ℹ️ Статус", callback_data="status")],
    ])


def _open_analytics_storage(now: datetime) -> AnalyticsStorage:
    """Single seam for opening the permanent analytics database -- tests
    monkeypatch this to point at an isolated tempfile database instead of
    the real production one, same pattern as _open_football_cache."""
    return AnalyticsStorage(now=now)


async def handle_statistics(query) -> None:
    """Public '📈 Статистика' button -- a short, non-technical report
    (no rationale/internal reasoning, just headline numbers)."""
    now_dt = datetime.now(timezone.utc)
    storage = _open_analytics_storage(now_dt)
    try:
        text = await asyncio.to_thread(compact_report, storage, stake=DEFAULT_STAKE, now=now_dt)
    finally:
        storage.close()
    await query.message.reply_text(text, reply_markup=main_keyboard())


async def admin_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/admin_report -- admin-only detailed statistics (breakdown by
    league/market/signal level, 30d/90d/all-time, trend), plus CSV and
    Excel exports of the complete permanent prediction history."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("⛔ Эта команда доступна только администратору.")
        return

    now_dt = datetime.now(timezone.utc)
    storage = _open_analytics_storage(now_dt)
    try:
        text = await asyncio.to_thread(admin_report, storage, stake=DEFAULT_STAKE, now=now_dt)
        await update.message.reply_text(text)

        csv_path = os.path.join(tempfile.gettempdir(), f"analytics_export_{now_dt.strftime('%Y%m%d_%H%M%S')}.csv")
        xlsx_path = os.path.join(tempfile.gettempdir(), f"analytics_export_{now_dt.strftime('%Y%m%d_%H%M%S')}.xlsx")
        await asyncio.to_thread(export_csv, storage, csv_path)
        await asyncio.to_thread(export_excel, storage, xlsx_path)
        with open(csv_path, "rb") as f:
            await update.message.reply_document(document=f, filename=os.path.basename(csv_path))
        with open(xlsx_path, "rb") as f:
            await update.message.reply_document(document=f, filename=os.path.basename(xlsx_path))
    finally:
        storage.close()


async def _analytics_result_checker_loop(app: Application) -> None:
    """Background task (started from Application.post_init, no extra
    job-queue dependency needed): periodically checks pending analytics
    predictions for finished fixtures and settles them. Never spends
    API-Football requests beyond FootballCache's existing daily reserve,
    and never touches the daily archive/prediction pipeline."""
    interval_seconds = RESULT_CHECK_INTERVAL_MINUTES * 60
    while True:
        try:
            now_dt = datetime.now(timezone.utc)
            football_cache = _open_football_cache(now_dt)
            storage = _open_analytics_storage(now_dt)
            try:
                summary = await asyncio.to_thread(
                    run_check_cycle, storage, football_cache, FOOTBALL_API_KEY, now_dt, stake=DEFAULT_STAKE,
                )
                if summary["checked"]:
                    print(f"[analytics] result checker: {summary}")
            finally:
                football_cache.close()
                storage.close()
        except Exception as exc:
            print(f"[analytics] result checker error: {exc}")
        await asyncio.sleep(interval_seconds)


async def _post_init(app: Application) -> None:
    asyncio.create_task(_analytics_result_checker_loop(app))


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
    """/status -- same diagnostics as the 'ℹ️ Статус' button, personalised
    to the requesting user (shown/remaining counts in the daily-pool section)."""
    caller_id = update.effective_user.id if update.effective_user else None
    await update.message.reply_text(build_status_text(telegram_user_id=caller_id), reply_markup=main_keyboard())


async def refresh_data_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/refresh_data -- admin-only. Requirement 8: archive refresh is only
    ever allowed automatically (once per 24h, lazily on the first regular
    button press) or via this explicit, confirmation-gated admin action
    that clearly warns about real quota spend. Never wired to the normal
    "🤖 Прогнозы ИИ"/"ℹ️ Статус" buttons."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("⛔ Эта команда доступна только администратору.")
        return

    remaining_text = "неизвестно"
    if FOOTBALL_API_KEY:
        try:
            football_cache = _open_football_cache(datetime.now(timezone.utc))
            remaining_text = str(football_cache.requests_available())
            football_cache.close()
        except Exception:
            pass

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚠️ Подтвердить обновление", callback_data=ADMIN_REFRESH_CONFIRM_PREFIX),
    ]])
    await update.message.reply_text(
        "⚠️ Это принудительно обновит суточный архив прогнозов ИИ и обратится к API-Football, "
        f"даже если текущий архив ещё не устарел. Осталось запросов сегодня: {remaining_text}.\n\n"
        "Подтвердить обновление?",
        reply_markup=keyboard,
    )


async def send_cached(query, prefix: str) -> None:
    entry = cache[prefix]
    await query.message.reply_text(
        "🗄 Данные из кэша (не старше 30 минут), новый запрос к API не выполнялся.\n\n"
        + entry["message"],
        reply_markup=main_keyboard(),
    )
    with open(entry["csv_path"], "rb") as f:
        await query.message.reply_document(document=f, filename=os.path.basename(entry["csv_path"]))


def _format_archive_header(generated_at: datetime, *, stale: bool = False) -> str:
    label = "Данные из суточного архива" if not stale else "Архив ещё обновляется, показаны последние сохранённые данные"
    when = format_user_time(generated_at)
    return f"💾 {label}\nОбновлено: {when}"


async def _reply_from_pool(
    query, archive: DailyArchive, now_dt: datetime, football_cache: FootballCache,
    telegram_user_id: Optional[int], *, stale: bool = False,
) -> dict:
    """Re-selects from the archived pool for the CURRENT moment (2026-07-15
    change) instead of replaying a static message: excludes fixtures that
    already started/finished, start within the minimum lead time, or were
    already shown to THIS Telegram user earlier today (per-user
    shown-tracking, same-day change), keeps the best up-to-5 of whatever
    real candidates remain, and persists any newly-eligible pick to
    tracking/analytics + this user's shown-history -- all pure CPU, zero
    API-Football/Odds API calls. Returns the diagnostics dict to cache for
    /status, with `recommendations_count` reflecting THIS re-selection."""
    messages, selected_entries, saved, duplicates = await asyncio.to_thread(
        reselect_from_archive, archive.pool, archive.diagnostics, now_dt,
        football_cache=football_cache, telegram_user_id=telegram_user_id,
    )
    messages = messages or ["На ближайшие 36 часов подходящих сигналов не найдено."]
    if not selected_entries and messages == [render_nothing_left_for_user_message()]:
        # Exact required text, sent standalone -- no archive header, it
        # already explains the situation in full.
        await query.message.reply_text(messages[0], reply_markup=main_keyboard())
    else:
        header = _format_archive_header(archive.generated_at, stale=stale)
        await query.message.reply_text(header + "\n\n" + messages[0], reply_markup=main_keyboard())
        for extra in messages[1:]:
            await query.message.reply_text(extra, reply_markup=main_keyboard())
    return {
        **archive.diagnostics,
        "recommendations_count": len(selected_entries),
        "saved_count": saved,
        "duplicate_count": duplicates,
        "source": "архив",
    }


async def handle_ai_predictions(query, *, force_refresh: bool = False) -> None:
    """Strict daily archive: the first successful run of the (rolling)
    24h window computes and persists the FULL real, odds-backed candidate
    pool once (2026-07-15 change); every later press within that window
    -- from any process, even across a bot restart -- re-selects the best
    still-startable picks from that SAME pool for the current moment
    (never calling API-Football/The Odds API again), so matches that have
    already kicked off are dropped and, if any real candidates remain,
    fresh ones automatically take their place. `force_refresh=True` is
    only ever passed from the admin-confirmed /refresh_data flow."""
    global ai_predictions_cache, ai_predictions_last_diagnostics, ai_predictions_last_success_ts

    # API-Football is the only REQUIRED key -- The Odds API is optional
    # coefficient enrichment only (production v3).
    if not FOOTBALL_API_KEY:
        await query.message.reply_text(
            "❌ Для прогнозов ИИ нужен ключ: FOOTBALL_API_KEY.",
            reply_markup=main_keyboard(),
        )
        return

    telegram_user_id = query.from_user.id if query.from_user else None

    now_dt = datetime.now(timezone.utc)
    football_cache = _open_football_cache(now_dt)
    try:
        if not force_refresh:
            archive = load_daily_archive(football_cache, now_dt)
            if archive is not None:
                ai_predictions_cache = {"archive": archive}
                ai_predictions_last_diagnostics = await _reply_from_pool(
                    query, archive, now_dt, football_cache, telegram_user_id,
                )
                return

        if ai_predictions_lock.locked() or is_refresh_in_progress(football_cache, now_dt):
            stale = load_daily_archive(football_cache, now_dt, ignore_ttl=True)
            if stale is not None:
                await _reply_from_pool(query, stale, now_dt, football_cache, telegram_user_id, stale=True)
            else:
                await query.message.reply_text(
                    "⏳ Архив данных уже формируется. Подожди немного и нажми кнопку снова.",
                    reply_markup=main_keyboard(),
                )
            return

        async with ai_predictions_lock:
            # Re-check now that we hold the lock, in case another task in
            # this same process just finished filling the archive.
            if not force_refresh:
                archive = load_daily_archive(football_cache, now_dt)
                if archive is not None:
                    ai_predictions_cache = {"archive": archive}
                    ai_predictions_last_diagnostics = await _reply_from_pool(
                        query, archive, now_dt, football_cache, telegram_user_id,
                    )
                    return

            mark_refresh_in_progress(football_cache, now_dt)
            intro = (
                "🤖 Формирую суточный архив прогнозов по данным API-Football... Это может занять минуту."
                if not force_refresh else
                "⚠️ Администратор запросил принудительное обновление архива. Обращаюсь к API-Football..."
            )
            await query.message.reply_text(intro)
            try:
                result = await asyncio.to_thread(
                    run_football_predictions,
                    football_cache=football_cache, now=now_dt, telegram_user_id=telegram_user_id,
                )
                messages = result.telegram_messages or ["На ближайшие 36 часов подходящих сигналов не найдено."]
                save_daily_archive(football_cache, result, now_dt)

                # Requirement: never report success unless the archive is
                # confirmed to actually be on disk in SQLite. Verify via a
                # SEPARATE connection to the same db file (not the one that
                # just wrote it) so this is a real read-back, not just a
                # same-connection cache hit.
                verify_cache = FootballCache(db_path=football_cache.db_path, now=now_dt)
                try:
                    verified_archive = load_daily_archive(verify_cache, now_dt, ignore_ttl=True)
                finally:
                    verify_cache.close()
                if verified_archive is None:
                    raise RuntimeError(
                        "Суточный архив не подтверждён в SQLite после записи -- запись не выполнена."
                    )
                archive = verified_archive

                ai_predictions_cache = {"archive": archive}
                # Full technical diagnostics are kept only for /status --
                # never sent here.
                ai_predictions_last_diagnostics = {
                    "found_fixtures": result.found_fixtures,
                    "matched_fixtures": result.matched_fixtures,
                    "unmatched_fixtures_no_odds": result.unmatched_fixtures_no_odds,
                    "analysed_fixtures": result.analysed_fixtures,
                    "fully_stat_fixtures": result.fully_stat_fixtures,
                    "recommendations_count": result.recommendations_count,
                    "excluded_no_real_odds_count": result.excluded_no_real_odds_count,
                    "api_football_requests_used": result.api_football_requests_used,
                    "api_football_requests_remaining": result.api_football_requests_remaining,
                    "api_football_requests_used_today": result.api_football_requests_used_today,
                    "odds_status": result.odds_status,
                    "odds_api_sports_queried": result.odds_api_sports_queried,
                    "odds_api_credits_remaining": result.odds_api_credits_remaining,
                    "odds_api_last_request_at": result.odds_api_last_request_at,
                    "errors": result.errors,
                    "source": "новый запрос",
                }
                ai_predictions_last_success_ts = now_dt.timestamp()
                for message in messages:
                    await query.message.reply_text(message, reply_markup=main_keyboard())
            except Exception as e:
                await query.message.reply_text(f"❌ Ошибка при формировании прогнозов ИИ: {e}", reply_markup=main_keyboard())
    finally:
        football_cache.close()


async def handle_live_predictions(query) -> None:
    """"🔴 Live" button (2026-07-15, Task #11): fully independent of
    "🤖 Прогнозы ИИ" -- its own cache key, its own lock, its own
    tracking/analytics mode marker (mode="live"). Never touches the
    shared daily archive/pool and never blocks (or is blocked by) the
    pre-match flow's lock."""
    global live_predictions_last_diagnostics

    if not FOOTBALL_API_KEY:
        await query.message.reply_text(
            "❌ Для Live-режима нужен ключ: FOOTBALL_API_KEY.",
            reply_markup=main_keyboard(),
        )
        return

    if live_predictions_lock.locked():
        await query.message.reply_text(
            "⏳ Live-данные уже обновляются. Подожди немного и нажми кнопку снова.",
            reply_markup=main_keyboard(),
        )
        return

    async with live_predictions_lock:
        now_dt = datetime.now(timezone.utc)
        football_cache = _open_football_cache(now_dt)
        try:
            try:
                result = await asyncio.to_thread(
                    run_live_predictions_cached,
                    football_cache=football_cache, now=now_dt, ttl_minutes=LIVE_CACHE_TTL_MINUTES,
                )
                live_predictions_last_diagnostics = {
                    "live_fixture_count": result.live_fixture_count,
                    "matched_fixture_count": result.matched_fixture_count,
                    "recommendations_count": result.recommendations_count,
                    "saved_count": result.saved_count,
                    "duplicate_count": result.duplicate_count,
                    "errors": result.errors,
                    "from_cache": result.from_cache,
                    "generated_at": result.generated_at.isoformat() if result.generated_at else None,
                }
                messages = result.telegram_messages or ["Live-сигналов сейчас нет."]
                for message in messages:
                    await query.message.reply_text(message, reply_markup=main_keyboard())
            except Exception as e:
                await query.message.reply_text(f"❌ Ошибка при получении Live-прогнозов: {e}", reply_markup=main_keyboard())
        finally:
            football_cache.close()


async def reset_shown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reset_shown -- admin-only. Clears only the CALLING admin's own
    per-user shown-history for the current Yekaterinburg calendar day, so
    they can re-verify the "🤖 Прогнозы ИИ" re-selection flow without
    waiting for the next daily update. Never touches the shared daily
    pool, tracking, or analytics data -- only the shown_picks rows for
    (today, this admin's Telegram id)."""
    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in ADMIN_TELEGRAM_IDS:
        await update.message.reply_text("⛔ Эта команда доступна только администратору.")
        return

    now_dt = datetime.now(timezone.utc)
    football_cache = _open_football_cache(now_dt)
    try:
        cleared = football_cache.clear_shown_for_user(local_date_str(now_dt), user_id)
    finally:
        football_cache.close()

    await update.message.reply_text(
        f"✅ Ваша история показанных сегодня прогнозов очищена (записей удалено: {cleared}). "
        "Общий суточный пул и статистика не затронуты."
    )


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


def build_status_text(telegram_user_id: Optional[int] = None) -> str:
    """User-facing status page. /status only ever READS persisted state
    (daily archive, quota counter -- plain SQLite reads) -- never calls
    fixture discovery or any API-Football endpoint, so pressing "ℹ️ Статус"
    can never spend quota.

    `telegram_user_id`, when provided, enables the per-user shown/remaining
    counts in the daily-pool section (how many picks this specific user has
    already seen today, and how many fresh ones remain for them)."""
    now_dt = datetime.now(timezone.utc)

    # --- Collect all data from persisted state (read-only) ---
    requests_remaining_text = "неизвестно"
    requests_used_today_text = "неизвестно"
    pool_total = 0
    pool_available = 0    # not started, enough lead time
    pool_started = 0      # kicked off / too close
    shown_count: Optional[int] = None
    available_for_user: Optional[int] = None
    archive_state_text = "отсутствует"
    last_update_text = "ещё не было успешных обновлений"
    next_update_text = "неизвестно"
    source_text = "нет данных"
    odds_api_credits_remaining_text = "неизвестно"
    odds_api_last_request_text = "запросов ещё не было"
    archive_error_notes: List[str] = []

    if FOOTBALL_API_KEY:
        try:
            football_cache = _open_football_cache(now_dt)
            requests_remaining_text = str(football_cache.requests_available())
            requests_used_today_text = str(football_cache.requests_used_today())
            # Allow a stale-calendar-day archive to be read here --
            # /status reports on it honestly, never presents it as fresh.
            archive = load_daily_archive(
                football_cache, now_dt, ignore_ttl=True, allow_stale_calendar_day=True,
            )

            if archive is not None:
                # Pool counts: how many entries are still bookable vs. kicked off.
                for rec, _, _ in archive.pool:
                    if rec.candidate.fixture.kickoff_utc > now_dt + timedelta(minutes=MIN_LEAD_TIME_MINUTES):
                        pool_available += 1
                    else:
                        pool_started += 1
                pool_total = pool_available + pool_started

                # Per-user counts (only when the caller supplies a Telegram id).
                if telegram_user_id is not None:
                    shown_keys = football_cache.get_shown_keys(
                        local_date_str(now_dt), telegram_user_id,
                    )
                    shown_count = len(shown_keys)
                    available_for_user = sum(
                        1 for rec, _, _ in archive.pool
                        if rec.candidate.fixture.kickoff_utc > now_dt + timedelta(minutes=MIN_LEAD_TIME_MINUTES)
                        and (rec.candidate.fixture.fixture_id, rec.candidate.market_key) not in shown_keys
                    )

                # Freshness label.
                is_fresh = (
                    not archive.is_stale_calendar_day
                    and (now_dt - archive.generated_at) <= timedelta(hours=DAILY_ARCHIVE_TTL_HOURS)
                )
                if archive.is_stale_calendar_day:
                    archive_state_text = "устарел (другой день в Екатеринбурге), будет пересобран при следующем запросе"
                elif is_fresh:
                    archive_state_text = "актуален"
                else:
                    archive_state_text = "устарел (>24ч), будет обновлён при следующем запросе"

                last_update_text = format_user_time(archive.generated_at, now_dt)
                next_update_text = format_user_time(
                    archive.generated_at + timedelta(hours=DAILY_ARCHIVE_TTL_HOURS), now_dt,
                )

                # Source fix (requirement 3): archive.diagnostics["source"] is
                # always "новый запрос" (set at save time); the authoritative
                # "last response source" is in ai_predictions_last_diagnostics.
                # After a process restart that source in memory, infer from
                # whether a fresh valid archive exists (→ next response will
                # come from archive).
                if ai_predictions_last_diagnostics and "source" in ai_predictions_last_diagnostics:
                    raw_source = ai_predictions_last_diagnostics["source"]
                elif is_fresh:
                    raw_source = "архив"
                else:
                    raw_source = archive.diagnostics.get("source", "")

                if raw_source == "архив":
                    source_text = "✅ архив текущего дня"
                elif raw_source == "новый запрос":
                    source_text = "🔄 новый запрос"
                else:
                    source_text = raw_source or "нет данных"

                d = archive.diagnostics
                odds_api_credits_remaining_text = d.get("odds_api_credits_remaining") or "неизвестно"
                odds_raw = d.get("odds_api_last_request_at")
                if odds_raw:
                    try:
                        odds_api_last_request_text = format_user_time(
                            datetime.fromisoformat(odds_raw), now_dt,
                        )
                    except ValueError:
                        odds_api_last_request_text = "неизвестно"
                else:
                    odds_api_last_request_text = "запросов ещё не было"

                if d.get("errors"):
                    archive_error_notes = d["errors"][:3]

            football_cache.close()
        except Exception:
            pass

    # --- Assemble the message ---
    lines: List[str] = ["ℹ️ Статус AI Ставки", ""]

    # ── 📦 Daily pool ────────────────────────────────────────────────────
    lines.append("📦 Дневной пул прогнозов:")
    if pool_total > 0:
        lines.append(f"  Всего найдено: {pool_total}")
        lines.append(f"  Доступны сейчас: {pool_available}")
        lines.append(f"  Уже начались / слишком близко: {pool_started}")
        if shown_count is not None:
            lines.append(f"  Показано вам сегодня: {shown_count}")
            lines.append(f"  Ещё доступно для вас: {available_for_user}")
    else:
        lines.append("  Пул не сформирован")
    lines.append(f"  Состояние: {archive_state_text}")
    lines.append(f"  Последнее обновление: {last_update_text}")
    lines.append(f"  Следующее обновление: {next_update_text}")
    lines.append(f"  Источник прогнозов: {source_text}")
    lines.append(f"  Последний запрос к Odds API: {odds_api_last_request_text}")
    lines.append(f"  Остаток кредитов Odds API: {odds_api_credits_remaining_text}")
    if archive_error_notes:
        lines.append(f"  Примечания: {'; '.join(archive_error_notes)}")

    # ── 🔴 Live ──────────────────────────────────────────────────────────
    lines.append("")
    lines.extend(_live_status_lines(now_dt))

    # ── ⚙️ Diagnostics (technical / line cache) ──────────────────────────
    lines += [
        "",
        "⚙️ Диагностика:",
        f"  Telegram token: {'доступен' if TELEGRAM_BOT_TOKEN else 'отсутствует'}",
        f"  API-Football key: {'доступен' if FOOTBALL_API_KEY else 'отсутствует'}",
        f"  API-Football: использовано {requests_used_today_text}, осталось {requests_remaining_text} запросов",
        f"  The Odds API: {_odds_api_status_text()}",
        "",
        "  Кэш линии (30 минут, кнопки ⚽🎾🏒🎯):",
    ]
    for cache_line in cache_status_lines().splitlines():
        lines.append(f"    {cache_line}")

    return "\n".join(lines)


def _live_status_lines(now_dt: datetime) -> List[str]:
    """"🔴 Live" section of /status -- reads only the persisted Live cache
    (never triggers a fetch).  Falls back to the cached result on disk
    when the in-memory diagnostics dict is empty (e.g. after a restart)."""
    last_check_text = "нет данных"
    live_fixtures_text = "нет данных"
    matched_odds_text = "нет данных"
    source_text = "нет данных"
    live_errors: List[str] = []

    if FOOTBALL_API_KEY:
        try:
            football_cache = _open_football_cache(now_dt)
            age_seconds = cache_age_seconds(football_cache, now_dt)
            if age_seconds is not None:
                minutes = int(age_seconds // 60)
                secs = int(age_seconds % 60)
                fresh = age_seconds <= LIVE_CACHE_TTL_MINUTES * 60
                last_check_text = (
                    f"{minutes} мин {secs} сек назад "
                    f"({'актуален' if fresh else 'устарел, обновится при следующем нажатии'})"
                )
            # Prefer the richer in-memory diagnostics; fall back to the
            # serialised cache entry when those were cleared by a restart.
            diag = live_predictions_last_diagnostics
            if diag is None:
                cached_result = load_cached_live_result(
                    football_cache, ttl_minutes=LIVE_CACHE_TTL_MINUTES * 24 * 60,
                )
                if cached_result is not None:
                    diag = {
                        "live_fixture_count": cached_result.live_fixture_count,
                        "matched_fixture_count": cached_result.matched_fixture_count,
                        "errors": cached_result.errors,
                        "from_cache": True,
                    }
            football_cache.close()
        except Exception:
            diag = live_predictions_last_diagnostics  # keep whatever we had
    else:
        diag = live_predictions_last_diagnostics

    if diag:
        live_fixtures_text = str(diag.get("live_fixture_count", 0))
        matched_odds_text = str(diag.get("matched_fixture_count", 0))
        from_cache = diag.get("from_cache", False)
        source_text = "кэш (Odds API)" if from_cache else "Odds API"
        if diag.get("errors"):
            live_errors = diag["errors"][:2]

    lines = [
        "🔴 Live (отдельный от суточного пула):",
        f"  Последняя проверка: {last_check_text}",
        f"  Live матчей найдено: {live_fixtures_text}",
        f"  Live коэффициентов найдено: {matched_odds_text}",
        f"  Источник: {source_text}",
    ]
    if live_errors:
        lines.append(f"  Примечания: {'; '.join(live_errors)}")
    return lines


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global last_known_credits
    query = update.callback_query
    await query.answer()

    if query.data == "status":
        caller_id = query.from_user.id if query.from_user else None
        await query.message.reply_text(build_status_text(telegram_user_id=caller_id), reply_markup=main_keyboard())
        return

    if query.data == AI_PREDICTIONS_PREFIX:
        await handle_ai_predictions(query)
        return

    if query.data == LIVE_PREDICTIONS_PREFIX:
        await handle_live_predictions(query)
        return

    if query.data == STATISTICS_PREFIX:
        await handle_statistics(query)
        return

    if query.data == ADMIN_REFRESH_CONFIRM_PREFIX:
        user_id = query.from_user.id if query.from_user else None
        if user_id not in ADMIN_TELEGRAM_IDS:
            await query.message.reply_text("⛔ Недостаточно прав для этого действия.")
            return
        await handle_ai_predictions(query, force_refresh=True)
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
    app.add_handler(CommandHandler("refresh_data", refresh_data_command))
    app.add_handler(CommandHandler("reset_shown", reset_shown_command))
    app.add_handler(CommandHandler("admin_report", admin_report_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.post_init = _post_init
    print("AI Ставки Bot запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
