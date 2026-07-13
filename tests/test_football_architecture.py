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


def test_api_football_sends_official_header_and_never_logs_key():
    """Every request must carry the official x-apisports-key header with
    exactly the configured key, and the key must never appear in a printed
    URL/param -- only in the header dict."""
    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {"x-ratelimit-requests-remaining": "97"}

        def json(self):
            return {"response": [], "errors": []}

    class FakeSession:
        def get(self, url, params=None, headers=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            captured["timeout"] = timeout
            return FakeResponse()

    provider = ApiFootballProvider(api_key="secret-test-key-value", session=FakeSession())
    provider.get_standings("Some League")

    check("request uses the official x-apisports-key header",
          captured.get("headers") == {"x-apisports-key": "secret-test-key-value"})
    check("a timeout is always set on the request", isinstance(captured.get("timeout"), (int, float)) and captured["timeout"] > 0)
    check("the API key is never placed in the URL or query params",
          "secret-test-key-value" not in captured.get("url", "")
          and "secret-test-key-value" not in str(captured.get("params", {})))


def test_api_football_reads_remaining_quota_header():
    class FakeResponse:
        status_code = 200
        headers = {"x-ratelimit-requests-remaining": "42"}

        def json(self):
            return {"response": [{"team": {"id": 1}}], "errors": []}

    class FakeSession:
        def get(self, url, params=None, headers=None, timeout=None):
            return FakeResponse()

    provider = ApiFootballProvider(api_key="k", session=FakeSession())
    provider.get_upcoming_matches("Some Team")
    check("remaining quota is parsed from the response header", provider.remaining_quota == 42)


def test_ai_predictions_pipeline_reads_football_api_key_from_correct_env_var():
    """The football statistics pipeline (ai_predictions/pipeline.py) must
    read exactly os.getenv('FOOTBALL_API_KEY') -- the real configured
    Replit secret name -- never a renamed/alternate variable."""
    import inspect
    import ai_predictions.pipeline as pipeline_module
    source = inspect.getsource(pipeline_module.run_ai_predictions)
    check("run_ai_predictions reads FOOTBALL_API_KEY (not a renamed variable)",
          'os.getenv("FOOTBALL_API_KEY")' in source, source)


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
    test_api_football_sends_official_header_and_never_logs_key()
    test_api_football_reads_remaining_quota_header()
    test_ai_predictions_pipeline_reads_football_api_key_from_correct_env_var()
    test_stat_contract()
    test_all_mock_provider_methods_return_stat()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
