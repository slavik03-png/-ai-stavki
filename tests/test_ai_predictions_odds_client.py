"""
Unit tests for ai_predictions/odds_client.py's dynamic market fallback:
when The Odds API rejects part of the requested market set with HTTP 422
and names exactly which markets are unsupported, the client must drop
only those and retry with the rest -- never fall all the way back to the
minimal set (losing spreads/h2h/totals) just because an unrelated market
(e.g. team_totals) is unavailable on the current plan.
"""

import sys

sys.path.insert(0, ".")

import ai_predictions.odds_client as odds_client_mod
from ai_predictions.odds_client import _parse_unsupported_markets, fetch_football_events

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


def test_parse_unsupported_markets_message():
    body = '{"message":"Markets not supported by this endpoint: btts, double_chance, draw_no_bet, team_totals","error_code":"INVALID_MARKET"}'
    parsed = _parse_unsupported_markets(body)
    check("parses every named unsupported market", parsed == ["btts", "double_chance", "draw_no_bet", "team_totals"], parsed)


def test_parse_returns_none_for_unrelated_error_body():
    check("returns None when the body has no unsupported-markets message", _parse_unsupported_markets("not json at all") is None)


def test_fetch_retries_with_remaining_markets_on_422(monkeypatch=None):
    calls = []

    def fake_fetch(sport_key, api_key, markets):
        calls.append(markets)
        if markets == odds_client_mod.PREFERRED_MARKETS:
            body = '{"message":"Markets not supported by this endpoint: btts, double_chance, draw_no_bet, team_totals"}'
            return None, "300", "HTTP 422", body
        # The retry should have dropped exactly the 4 unsupported markets,
        # keeping h2h/totals/spreads intact.
        expected_remaining = "h2h,totals,spreads"
        if markets == expected_remaining:
            return [{"id": "evt-1", "bookmakers": []}], "299", None, None
        return None, "299", f"unexpected retry markets {markets}", None

    original = odds_client_mod._fetch_one_league
    odds_client_mod._fetch_one_league = fake_fetch
    try:
        events, credits, errors = fetch_football_events(api_key="fake-key", sport_keys=["soccer_epl"])
    finally:
        odds_client_mod._fetch_one_league = original

    check("first call used the full preferred market set", calls[0] == odds_client_mod.PREFERRED_MARKETS, calls)
    check("retry dropped only the 4 named unsupported markets, kept h2h/totals/spreads", calls[1] == "h2h,totals,spreads", calls)
    check("events from the successful retry are returned", len(events) == 1, events)
    check("no error is surfaced once the retry succeeds", errors == [], errors)


def run():
    test_parse_unsupported_markets_message()
    test_parse_returns_none_for_unrelated_error_body()
    test_fetch_retries_with_remaining_markets_on_422()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
