---
name: Odds-first fixture gating (supersedes API-Football-primary rule)
description: Since 2026-07-15, real Odds API coverage GATES which fixtures get analysed at all — this deliberately reverses the earlier "Odds API must never gate candidate creation" rule.
---

## The decision

As of 2026-07-15, `ai_predictions/football_pipeline.py::run_football_predictions` fetches all
active Odds API football events and matches them to API-Football fixtures **before** any
statistics analysis happens. Only fixtures with a confident real-event match are analysed;
unmatched fixtures are counted (`FootballPipelineResult.unmatched_fixtures_no_odds`) and skipped
entirely, never spending API-Football budget on them.

This is a deliberate reversal of the older rule in `api-football-primary-architecture.md`
("Odds API must never gate candidate creation, only optional price display"). Both rules were
correct for the product goal they were written for — they are not a contradiction to "fix", they
are two different valid architectures depending on what the product wants right now:

- **API-Football-primary** (pre-2026-07-15): maximizes recommendation *volume* even when bookmaker
  coverage is thin/zero — useful if showing an estimate without a price is acceptable.
- **Odds-first gating** (2026-07-15 onward, current): guarantees every analysed fixture can
  actually become a real, price-backed recommendation, and avoids wasting the
  `MAX_FIXTURES_ANALYSED_PER_RUN` analysis cap on fixtures that were never bookable anyway. Chosen
  because the explicit product goal became "deliver 3-5 real, priced recommendations a day, never
  analyse matches that aren't in the betting line at all."

**Why:** the user explicitly asked to reverse the previous architecture for this reason. If a
future request asks to go back to "analyse everything, treat odds as optional", that is also a
legitimate, previously-implemented mode — check with the user which product goal is current before
assuming either rule by default.

**How to apply:** when touching `run_football_predictions`, remember the odds fetch+match
(`ai_predictions/odds_client.fetch_all_active_football_events` +
`ai_predictions/fixture_matching.match_fixtures_to_events`) happens exactly once per run and its
result (`match_result.matches`) is reused both to decide which fixtures to analyse AND, via
`ai_predictions/odds_lookup.attach_prices_from_matches`, to price the final picks — never
fetch/match a second time (that's what `lookup_coefficients` used to do internally; it's now a thin
wrapper kept for callers that want the old all-in-one behavior).

A caller-provided empty/None `odds_api_key` must short-circuit before calling
`fetch_all_active_football_events` at all — that function's own fallback
(`api_key = api_key or _get_odds_api_key()`) will silently substitute the real environment secret
for a falsy argument, which is surprising in tests that pass `odds_api_key=""` to simulate "no key
configured" and will burn real quota if a real `ODDS_API_KEY` secret happens to be set.
