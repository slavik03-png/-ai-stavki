"""
Confirms the ai_predictions/ orchestration boundary:
- bot.py DOES import ai_predictions (it is the one integration point);
- bot.py still does NOT import tracking or selection_engine directly;
- ai_predictions/ does NOT import bot.py or telegram (no reverse dependency);
- ai_predictions/ is the one package allowed to import football/,
  selection_engine/, tracking/ and perform network calls together.
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


def test_bot_imports_ai_predictions_but_not_tracking_or_selection_engine():
    modules = _imported_modules(Path("bot.py"))
    check("bot.py imports ai_predictions", "ai_predictions" in modules, modules)
    check("bot.py still does not import selection_engine", "selection_engine" not in modules, modules)
    check("bot.py still does not import tracking", "tracking" not in modules, modules)


def test_ai_predictions_does_not_import_bot_or_telegram():
    package_dir = Path("ai_predictions")
    offending = []
    for py_file in sorted(package_dir.glob("*.py")):
        modules = _imported_modules(py_file)
        if "bot" in modules or "telegram" in modules:
            offending.append(str(py_file))
    check("no ai_predictions module imports bot.py or telegram", offending == [], offending)


def test_ai_predictions_uses_all_three_packages():
    package_dir = Path("ai_predictions")
    seen = set()
    for py_file in sorted(package_dir.glob("*.py")):
        seen |= _imported_modules(py_file)
    for expected in ("football", "selection_engine", "tracking"):
        check(f"ai_predictions reuses {expected}", expected in seen, seen)


def run():
    test_bot_imports_ai_predictions_but_not_tracking_or_selection_engine()
    test_ai_predictions_does_not_import_bot_or_telegram()
    test_ai_predictions_uses_all_three_packages()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
