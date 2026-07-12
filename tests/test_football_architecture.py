"""
Architecture-level sanity tests for the provider-agnostic football
statistics layer (football/interface.py + football/providers/*).

These tests confirm the contract itself is sound, independent of the
prediction/recommendation/report logic built on top of it:
- every provider method returns a Stat (never a raw value or None)
- Stat.missing() never carries a value, and always carries a reason
- Stat.ok() always carries a value
- the mock provider implements the full abstract interface
- ApiFootballProvider (activated for the AI predictions feature) makes
  real network calls when a key is configured, but degrades safely to
  Stat.missing(...) -- never a crash, never invented data -- when no key
  is configured or the network/API fails
"""

import inspect
import sys

sys.path.insert(0, ".")

from football.interface import FootballStatisticsProvider, Stat
from football.providers.mock_provider import MockFootballProvider
from football.providers.api_football import ApiFootballProvider

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def _abstract_methods():
    return [
        name for name, member in inspect.getmembers(FootballStatisticsProvider)
        if getattr(member, "__isabstractmethod__", False)
    ]


def test_mock_provider_implements_full_interface():
    missing = [m for m in _abstract_methods() if not hasattr(MockFootballProvider, m)]
    check("MockFootballProvider implements every abstract method", not missing, missing)


def test_api_football_template_implements_full_interface():
    missing = [m for m in _abstract_methods() if not hasattr(ApiFootballProvider, m)]
    check("ApiFootballProvider template implements every abstract method", not missing, missing)


def test_api_football_degrades_safely_without_key():
    provider = ApiFootballProvider()  # must not require any secret/env var to construct
    stat = provider.get_last_matches("Any Team")
    check("ApiFootballProvider without a key returns Stat.missing, never a crash",
          isinstance(stat, Stat) and not stat.available and bool(stat.reason))
    stat2 = provider.get_standings("Any League")
    check("ApiFootballProvider without a key returns Stat.missing for every method",
          isinstance(stat2, Stat) and not stat2.available and bool(stat2.reason))


def test_api_football_enforces_request_budget():
    provider = ApiFootballProvider(api_key="fake-key-for-budget-test", max_requests_per_run=0)
    stat = provider.get_last_matches("Any Team")
    check("ApiFootballProvider with an exhausted budget returns Stat.missing without a network call",
          isinstance(stat, Stat) and not stat.available and "лимит" in (stat.reason or "").lower())


def test_stat_contract():
    ok = Stat.ok(42)
    missing = Stat.missing("no data")
    check("Stat.ok carries a value and available=True", ok.available and ok.value == 42 and ok.reason is None)
    check("Stat.missing carries no value and a reason", not missing.available and missing.value is None and bool(missing.reason))


def test_all_mock_provider_methods_return_stat():
    provider = MockFootballProvider()
    calls = {
        "get_upcoming_matches": lambda: provider.get_upcoming_matches("Mock Home FC"),
        "get_last_matches": lambda: provider.get_last_matches("Mock Home FC"),
        "get_home_away_form": lambda: provider.get_home_away_form("Mock Home FC"),
        "get_head_to_head": lambda: provider.get_head_to_head("Mock Home FC", "Mock Away FC"),
        "get_goals_by_half": lambda: provider.get_goals_by_half("Mock Home FC"),
        "get_btts_frequency": lambda: provider.get_btts_frequency("Mock Home FC"),
        "get_clean_sheets": lambda: provider.get_clean_sheets("Mock Home FC"),
        "get_corners": lambda: provider.get_corners("Mock Home FC"),
        "get_fouls": lambda: provider.get_fouls("Mock Home FC"),
        "get_cards": lambda: provider.get_cards("Mock Home FC"),
        "get_shots": lambda: provider.get_shots("Mock Home FC"),
        "get_standings": lambda: provider.get_standings("Mock League"),
        "get_lineups": lambda: provider.get_lineups("Mock Home FC", "Mock Away FC"),
        "get_injuries": lambda: provider.get_injuries("Mock Home FC"),
    }
    bad = [name for name, fn in calls.items() if not isinstance(fn(), Stat)]
    check("every MockFootballProvider method returns a Stat", not bad, bad)


def run():
    test_mock_provider_implements_full_interface()
    test_api_football_template_implements_full_interface()
    test_api_football_degrades_safely_without_key()
    test_api_football_enforces_request_budget()
    test_stat_contract()
    test_all_mock_provider_methods_return_stat()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
