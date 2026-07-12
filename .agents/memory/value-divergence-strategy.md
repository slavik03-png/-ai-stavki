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
