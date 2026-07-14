---
name: API-Football-primary production architecture (v3)
description: Why and how the Telegram bot's recommendations were rearchitected so The Odds API can never gate or reduce them.
---

As of 2026-07-14, the bot's live "🤖 Прогнозы ИИ" recommendations run on
`ai_predictions/football_pipeline.py`, NOT the older fixture-discovery
+ Odds API divergence pipeline (`ai_predictions/value_pipeline.py`,
described in value-divergence-strategy.md). That older pipeline is
odds-driven by construction — every candidate starts from a matched
bookmaker row — so it structurally cannot produce a recommendation when
The Odds API quota is exhausted, even though real fixtures exist. This
is exactly the failure that forced the rewrite (production outage: 0
recommendations while Odds API quota was 0).

**Rule going forward: The Odds API must never be a dependency for
candidate creation, only for optional price display.** Any future
prediction feature must build its probability/candidate entirely from
API-Football data first (predictions endpoint + recent-form goal
averages via an independent-Poisson model for totals/BTTS), and treat a
bookmaker coefficient as a best-effort decoration attached afterward
(`ai_predictions/odds_lookup.py`) that degrades to "нет данных" on ANY
failure (no key, 401, exhausted quota, no match) without raising or
removing the recommendation.

**Why:** the whole point of the fix was resilience to Odds API outages;
re-coupling candidate creation to Odds API data in a future change would
silently reintroduce the same production outage.

**How to apply:** when extending recommendation logic, check whether the
new signal needs a live bookmaker price to be *created* (forbidden) vs.
merely *displayed alongside* (fine, via the optional-enrichment pattern).

## Quota-reserve gating must live per-HTTP-call, never per-fixture

An earlier version of this pipeline pre-checked `can_spend(N)` once per
fixture *before* attempting any analysis at all, and aborted the entire
per-run loop the moment the daily reserve hit zero — this produced
`analysed=0, recommendations=0` even with 250+ real fixtures available,
because cached/free data for those fixtures was never even consulted.

**Fix:** every fixture in the analysis loop is always processed; the
reserve check moved down to wrap each individual real network call
(predictions fetch, per-team recent-form fetch) so a cache HIT never
touches the check at all, and a cache MISS with no budget left simply
degrades that one ingredient to "unavailable" instead of aborting the
fixture. A fixture with truly nothing real available (no cache, no
budget) still gets ranked via a historical-baseline fallback (real
aggregate football statistics, not fixture-specific), hard-capped at LOW
confidence via a `sample_size_category == "none"` check in the
classifier — never silently dropped, never presented above LOW.

**Why:** "quota exhausted" must degrade recommendation *confidence/count
per fixture*, not zero out the whole run — the two failure modes look
identical in a naive per-fixture pre-check but are very different in
user impact.

**How to apply:** any future rate-limited enrichment step should gate at
the smallest real unit of network cost (one HTTP call), read persistent
cache first, and provide a clearly-labelled reduced-confidence fallback
rather than an early `break`/abort of the whole batch.

## Daily quota reserve vs. hard cap

`ai_predictions/football_cache.py`'s `API_FOOTBALL_QUOTA_RESERVE` is an
app-level safety margin subtracted from the real API-imposed daily cap
(`API_FOOTBALL_QUOTA_RESERVE` requests are always left unspent by normal
runs). It is a soft policy value read into `football_cache.py`'s own
module namespace via `from value_config import ...`, so patching
`value_config.API_FOOTBALL_QUOTA_RESERVE` after import has no effect —
you must patch `football_cache.API_FOOTBALL_QUOTA_RESERVE` (or pass a
smaller value some other way) if a one-off validation run genuinely needs
to dip into the reserve.
