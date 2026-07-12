"""
Regression guard: re-runs every pre-existing test file (football, tracking,
bot) as a subprocess to confirm the new selection_engine package has not
broken anything that already worked.
"""

import subprocess
import sys
from pathlib import Path

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


PRE_EXISTING_TEST_FILES = [
    "tests/test_football_architecture.py",
    "tests/test_football_prediction.py",
    "tests/test_recommendation.py",
    "tests/test_report_ai.py",
    "tests/test_bot_regression.py",
    "tests/test_tracking_storage.py",
    "tests/test_tracking_settlement.py",
    "tests/test_tracking_statistics.py",
    "tests/test_tracking_report.py",
    "tests/test_tracking_result_checker.py",
    "tests/test_tracking_bot_isolation.py",
]


def run():
    for path in PRE_EXISTING_TEST_FILES:
        if not Path(path).exists():
            check(f"{path} exists", False)
            continue
        proc = subprocess.run([sys.executable, path], capture_output=True, text=True)
        check(f"{path} still passes unmodified", proc.returncode == 0,
              "" if proc.returncode == 0 else (proc.stdout[-500:] + proc.stderr[-500:]))

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
