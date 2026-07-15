---
name: Bookmaker coverage investigation & mandatory real-odds gating
description: Findings on which real-money bookmakers The Odds API / API-Football can and can't supply, and the product rule that replaced per-bookmaker checks — never show a prediction without a real, named bookmaker price.
---

## Bookmaker coverage findings (checked 2026-07-15, may drift over time)
- The Odds API's full bookmaker catalog (all regions: us, us2, uk, eu, fr, se, au) does not include Зенит, Melbet, Fonbet, or Winline.
- API-Football's `/odds/bookmakers` catalog lists Fonbet (id 33), but it is catalog-only: sampling 50 real fixtures across the plan's allowed date range showed Fonbet supplying 0 of 50 real odds responses. A bookmaker appearing in this catalog is not evidence it actually quotes live odds.
- odds-api.io sells a real "MelBet Sportsbook API" (Starter £99/mo, 5 bookmakers). No aggregator found offers Зенит/Fonbet/Winline as a licensed API — likely Russian-market-specific licensing gaps. Full findings in `reports/bookmaker_coverage_report.md`.

**Why this matters:** don't re-investigate these 4 bookmakers from scratch in a future session — the answer (no real coverage without a paid Melbet-only source, and scraping is excluded by policy) is already established.

## Product rule that replaced the per-bookmaker check
Instead of gating on specific bookmaker names, the user's final instruction was: only show a prediction when a real price was actually retrieved from *any* real bookmaker via the already-connected Odds API; never render a "нет данных"/placeholder coefficient; state which bookmaker the shown price came from on every card.

**Why:** "better 2-3 real bets than 5 predictions for events not in the betting line" — the user explicitly rejected showing predictions with no real, current market backing, even as a soft/labelled fallback.

**How to apply:** in `ai_predictions/football_pipeline.py`, a `RankedRecommendation` that clears the probability/completeness threshold is dropped entirely (never rendered, saved, or recorded in analytics) unless `odds_lookup.lookup_coefficients()` found a real matched price for that exact fixture+market. `odds_lookup.OddsLookupResult` carries both the price and the real bookmaker title (`bookmaker_by_fixture`) so cards can name the source. This is a deliberate, narrow exception to the older "Odds API never gates candidate creation" rule — it gates final *inclusion in output*, not the underlying stats-based candidate analysis.
