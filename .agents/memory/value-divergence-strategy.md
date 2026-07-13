---
name: Cross-bookmaker value-divergence strategy design
description: How ai_predictions/value_engine.py detects genuine betting value without any team-statistics model, and why spreads/handicap markets are excluded.
---

When a real statistics provider can't supply usable data (e.g. a free-tier
API-Football plan blocking the current season), computing "edge" as
(bookmaker-consensus-probability) vs (best-price-implied-probability) is
self-referential and always collapses to ~0 if the consensus includes the
best-price bookmaker itself.

**Fix:** use a leave-one-out consensus — average the margin-removed
implied probabilities of every bookmaker *except* the one offering the
best price — and compare that against the best price's own implied
probability. This is genuine cross-bookmaker divergence detection, not a
fabricated model probability, because every number is a real market price.

**Why:** the user explicitly required "no invented probabilities/edge" —
this is the only formulation of "edge" that satisfies that constraint
while still being non-trivial (self-referential consensus always nets to
~0 edge).

**How to apply:** any future value-betting/arbitrage feature that lacks a
real statistical model should use this leave-one-out pattern, not a
naive "best price vs all-bookmaker average" comparison.

**Spreads/Asian handicap markets are intentionally excluded** from this
strategy (only h2h/double_chance/draw_no_bet/totals are used) because
`tracking/settlement.py` has no handicap-settlement function — a spreads
recommendation could never be graded. Add a settlement function first if
spreads support is ever requested.

## Ranked HIGH/MEDIUM/LOW/REJECTED tiering (replaced the binary pass/reject filter)

All tier thresholds live in one place (`ai_predictions/value_config.py`) —
never hard-code an EV/edge/bookmaker-count number anywhere else.

- **Pre-dedup bookmaker counts need their own helper.** `matching.py`
  already dedupes bookmaker rows before `value_engine.py` ever sees them,
  so "how many bookmaker rows existed before dedup" (needed for a
  duplicate-quote diagnostic) can't be recovered downstream — it must be
  computed on the raw validated rows and threaded through explicitly,
  keyed by grouping point (not signed point) so spreads still line up.

- **A single high price must not dominate the ranking score.** Use
  `log2(bookmaker_count)` (diminishing returns) plus a dispersion penalty
  and a flat outlier penalty, never bookmaker_count or price directly —
  otherwise heavier market coverage or one big number always wins
  regardless of real EV/edge quality.

- **Outlier demotion cascades exactly one level per flag**
  (HIGH→MEDIUM→MEDIUM→LOW→REJECTED), applied *after* tier classification,
  not baked into the tier thresholds themselves — keeps the two concerns
  (divergence strength vs. "is this one quote suspicious") independently
  testable.

- **Cross-level de-dup bug found via testing:** a naive "already shown
  (event_id, market_type) pairs" check only blocks same-market duplicates
  — it does NOT enforce "a lower-tier signal never gets a second slot on
  an event that already has a stronger signal on a different market."
  That rule must compare the new candidate's tier against *every*
  already-shown candidate on that event, not just check market_type
  equality. Any future selector/ranking dedup logic should build the
  "already shown" set as candidates-per-event, not a flat tuple set.

- **Selection policy superseded: global top-5, not per-level cap-of-5.**
  The authoritative design (as of 2026-07-13) is: dedupe per event across
  ALL tiers at once, then rank every surviving candidate GLOBALLY —
  HIGH always before MEDIUM before LOW as the primary sort key (never
  blended with score across tiers) — and keep only the top 5 total. If
  zero qualify, surface the 5 REJECTED candidates with the highest
  ranking_score (closest to a real threshold) instead of an empty report.
  Do not regress to "up to 5 per level, 15 max" — that was the earlier,
  now-replaced design.
