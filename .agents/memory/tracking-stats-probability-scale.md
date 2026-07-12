---
name: tracking.statistics.Stats scale vs 0..1 probability conventions
description: tracking/statistics.py's Stats.win_rate and .roi are 0..100 percent, not 0..1 -- any consumer using a 0..1 probability convention must divide by 100.
---

`tracking.statistics.Stats.win_rate` and `.roi` are percentages on a 0..100
scale (e.g. `25.0` means 25%), computed directly as `x/decisive_count*100`.
Any module that adopts a 0..1 probability convention elsewhere (as
`selection_engine` does for `model_probability`, `edge`, etc.) must
explicitly rescale `win_rate` by dividing by 100 when reading it into a
0..1 field; `roi` stays a percent since it isn't a probability.

**Why:** easy to silently mix scales when pulling historical win-rate into
a probability-shaped field feeding a scoring formula -- an unscaled 25.0
would be clamped to 1.0 (100%) instead of 0.25, badly overstating
confidence.

**How to apply:** whenever code reads `Stats.win_rate` (from
`tracking.statistics.compute_statistics`/`by_market_type`/`by_model_version`/etc.)
into a field documented as 0..1, divide by 100 first.
