---
name: Confidence-blend missing-data trap
description: Why a deterministic confidence-scoring formula must never simply drop missing evidence from its weighted average.
---

When blending several sample-weighted ratios (e.g. team A's home form, team B's away form) into a single 0-100 confidence score, do not filter out factors with no real data before averaging. If a missing factor would likely have been unfavorable, dropping it lets the remaining (better) factors dominate the average — so a match with *less* data can score *higher* than one with full data, which is backwards.

**Why:** Caught via an explicit regression test ("missing data must never increase confidence relative to the same market with full data"). A flat missing-data penalty (a `-8 per missing item` subtraction) is not enough on its own to counteract this, because the underlying weighted-average shift can be larger than the flat penalty.

**How to apply:** When a factor is missing, blend it into the average at a neutral ratio (0.5) with a small fixed weight instead of omitting it. This dampens the average toward neutral (never toward "confirmed") and also grows the effective sample size, which further softens the sample-size confidence boost. Keep the flat per-missing-item penalty too — the two mechanisms are complementary, not redundant. Applies to any deterministic confidence/scoring formula built from a weighted average of partially-available signals, not just this sports-betting project.
