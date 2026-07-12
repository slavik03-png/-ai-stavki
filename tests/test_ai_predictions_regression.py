"""
Full regression guard for the AI predictions live launch: runs every
test_*.py file in tests/ (except itself) as a subprocess, confirming the
new ai_predictions/ package and its real API-Football/Odds API wiring have
not broken any previously-passing test.
"""

import subprocess
import sys
from pathlib import Path

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def run():
    this_file = Path(__file__).name
    test_files = sorted(
        p for p in Path("tests").glob("test_*.py")
        if p.name != this_file
    )
    for path in test_files:
        proc = subprocess.run([sys.executable, str(path)], capture_output=True, text=True)
        check(f"{path} passes", proc.returncode == 0,
              "" if proc.returncode == 0 else (proc.stdout[-800:] + proc.stderr[-800:]))

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed ({len(test_files)} files run)")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
