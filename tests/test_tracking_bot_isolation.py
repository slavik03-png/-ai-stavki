"""
Confirms the tracking package is fully isolated from bot.py: bot.py imports
nothing from tracking/, and tracking/ imports nothing from bot.py or
telegram. Byte-identity of bot.py itself is checked by the task runner
(md5 before/after), not here, since a test file can only observe the repo
state at the time it runs, not "before".
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


def test_bot_does_not_import_tracking():
    modules = _imported_modules(Path("bot.py"))
    check("bot.py does not import tracking", "tracking" not in modules, modules)


def test_tracking_modules_do_not_import_bot_or_telegram():
    tracking_dir = Path("tracking")
    offenders = []
    for py_file in tracking_dir.glob("*.py"):
        modules = _imported_modules(py_file)
        if "bot" in modules or "telegram" in modules:
            offenders.append(str(py_file))
    check("no tracking module imports bot.py or telegram", not offenders, offenders)


def run():
    test_bot_does_not_import_tracking()
    test_tracking_modules_do_not_import_bot_or_telegram()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
