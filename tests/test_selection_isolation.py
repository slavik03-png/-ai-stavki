"""
Confirms selection_engine/ is fully isolated: bot.py does not import it,
and it does not import bot.py or telegram. It is allowed (by design) to
import from tracking/ (for persistence/history reuse) and from football/
is NOT required and NOT currently used -- this test also documents that
choice so a future change to reuse football/ directly is a deliberate,
visible decision rather than an accident.
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


def _imported_modules(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_bot_does_not_import_selection_engine():
    modules = _imported_modules(Path("bot.py"))
    check("bot.py does not import selection_engine", "selection_engine" not in modules, modules)


def test_selection_engine_does_not_import_bot_or_telegram():
    package_dir = Path("selection_engine")
    offending = []
    for py_file in sorted(package_dir.glob("*.py")):
        modules = _imported_modules(py_file)
        if "bot" in modules or "telegram" in modules:
            offending.append(str(py_file))
    check("no selection_engine module imports bot.py or telegram", offending == [], offending)


def test_selection_engine_only_reuses_tracking_for_persistence():
    package_dir = Path("selection_engine")
    tracking_users = []
    for py_file in sorted(package_dir.glob("*.py")):
        modules = _imported_modules(py_file)
        if "tracking" in modules:
            tracking_users.append(str(py_file))
    # This is informative, not a hard requirement -- at least confirms the
    # package builds on tracking/ rather than inventing a second store.
    check("selection_engine reuses tracking/ rather than a second datastore", len(tracking_users) > 0, tracking_users)


def test_selection_engine_makes_no_network_calls():
    package_dir = Path("selection_engine")
    offending = []
    for py_file in sorted(package_dir.glob("*.py")):
        modules = _imported_modules(py_file)
        if modules & {"requests", "httpx", "aiohttp", "urllib"}:
            offending.append(str(py_file))
    check("selection_engine performs no HTTP/network calls", offending == [], offending)


def run():
    test_bot_does_not_import_selection_engine()
    test_selection_engine_does_not_import_bot_or_telegram()
    test_selection_engine_only_reuses_tracking_for_persistence()
    test_selection_engine_makes_no_network_calls()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
