---
name: Window-exclusion vs. broken matching, and Odds API market fallback
description: How to tell a genuinely empty (off-season/no-events-in-window) run apart from a broken matching/grouping pipeline, and how The Odds API's per-market 422 rejection should be handled.
---

## Symptom that looks like a bug but usually isn't
"Events received: N > 0" but "markets matched / candidates / recommendations: 0" for a
value-detection style pipeline (built on real bookmaker odds, not a stats
model) is very often **100% of events falling outside the mandatory
event-time window** (e.g. a 36h horizon), not a matching/grouping defect.
Off-season leagues can have every real event 4-6+ weeks out.

**Why:** a diagnostics report that doesn't prominently surface
`events_excluded_by_window` makes total window-exclusion visually
identical to a fully broken pipeline — this caused a false "matching is
broken" bug report even though the matching/grouping logic itself worked
correctly on real data once the window was widened.

**How to apply:** any pipeline with an event-time window must render the
excluded-by-window count in the top-level diagnostics, not just an
internal counter. Before assuming matching/grouping is broken, fetch one
real cached API response and re-run with `now` shifted so real events
fall inside the window — if candidates then appear, the pipeline is fine
and the real fix is diagnostics/reporting, not matching logic.

## The Odds API market-set rejection
Requesting multiple markets in one call (e.g.
`h2h,totals,btts,team_totals,draw_no_bet,double_chance`) can get rejected
with HTTP 422 `Markets not supported by this endpoint: <list>` even when
some of those markets normally work — plan/endpoint support for a market
can change. The response body names exactly which markets are rejected.

**How to apply:** parse the 422 body's market list and retry with just
those markets removed, instead of collapsing to a minimal fallback set —
otherwise you silently lose markets (e.g. `spreads`) that were never the
problem. Also: exchange-style bookmakers can return `h2h_lay` unprompted
even when it isn't requested; treat any unrecognized market key as an
explicit "unsupported market" diagnostic count, never merge it into `h2h`.
