"""
Confirms the "🔴 Live" mode is fully isolated from the shared daily
archive/pool and never blocks the "🤖 Прогнозы ИИ" flow (Task #11
requirement 1): distinct cache keys, distinct in-process locks, and
bot.py wires both buttons to independent handler functions.
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, ".")

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def _source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_live_cache_key_disjoint_from_daily_archive_key():
    import ai_predictions.football_pipeline as football_pipeline_mod
    import ai_predictions.live_pipeline as live_pipeline_mod
    check(
        "Live's cache key differs from the shared daily archive key",
        live_pipeline_mod.LIVE_CACHE_KEY != football_pipeline_mod.DAILY_ARCHIVE_KEY,
    )
    check(
        "Live's cache key differs from the refresh-in-progress lock key",
        live_pipeline_mod.LIVE_CACHE_KEY != football_pipeline_mod.DAILY_ARCHIVE_LOCK_KEY,
    )


def test_live_pipeline_module_never_imports_football_pipeline():
    """Live's own orchestration module must not import football_pipeline.py
    at all -- if it needs something from there, that is a sign the two are
    not actually independent."""
    tree = ast.parse(_source("ai_predictions/live_pipeline.py"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    check(
        "live_pipeline.py does not import football_pipeline.py",
        "ai_predictions.football_pipeline" not in imported,
        imported,
    )


def test_bot_has_two_independent_locks_for_the_two_buttons():
    source = _source("bot.py")
    check("bot.py defines a separate live_predictions_lock", "live_predictions_lock" in source)
    check("bot.py defines a separate ai_predictions_lock", "ai_predictions_lock" in source)
    check(
        "the two locks are distinct names (never the same asyncio.Lock reused)",
        "live_predictions_lock = asyncio.Lock()" in source and "ai_predictions_lock = asyncio.Lock()" in source,
    )


def test_bot_wires_the_live_button_to_its_own_handler():
    source = _source("bot.py")
    check("bot.py defines a dedicated LIVE_PREDICTIONS_PREFIX callback", "LIVE_PREDICTIONS_PREFIX" in source)
    check("bot.py defines handle_live_predictions", "async def handle_live_predictions(" in source)
    check(
        "the Live callback dispatches to handle_live_predictions, not handle_ai_predictions",
        "if query.data == LIVE_PREDICTIONS_PREFIX:" in source
        and "await handle_live_predictions(query)" in source,
    )


def run():
    test_live_cache_key_disjoint_from_daily_archive_key()
    test_live_pipeline_module_never_imports_football_pipeline()
    test_bot_has_two_independent_locks_for_the_two_buttons()
    test_bot_wires_the_live_button_to_its_own_handler()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
