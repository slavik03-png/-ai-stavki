---
name: Dynamic football-league discovery via The Odds API
description: How to discover every active football competition (not a hardcoded league list) from The Odds API, and the caveats that matter when merging their events into one pool.
---

The Odds API's `GET /v4/sports` catalog lists every sport it currently covers with `key`, `group`, `active`, and `has_outrights` fields. Filtering to `group == "Soccer"` gives every real football competition The Odds API is covering *right now* — including lower leagues and minor cups, not just the 7 majors a hardcoded list would name.

**Why:** a hardcoded major-league list (EPL, La Liga, Serie A, Bundesliga, Ligue 1, UCL, UEL) misses most of the real 36h event inventory. In one live check, discovery found 34 active football competitions and 15 real events in the strict 36h window, vs. 7 leagues previously queried.

**How to apply:**
- Exclude `has_outrights: true` entries — those are tournament-winner futures markets with no per-match two-team price to compare, not a regular match.
- Exclude `active: false` entries — season not running, not a bug.
- If the `/sports` catalog call itself fails (network/HTTP/JSON error), fall back to the old hardcoded league list rather than returning zero football coverage for the run — and record the failure/fallback in diagnostics so it's visible, never silent.
- Cache the `/sports` catalog for ~1h (it changes rarely) and per-league odds responses for a few minutes, to avoid burning API credits on repeated runs within a short window. Never cache an error response — only a confirmed success (see the existing "transient error cache poisoning" lesson).
- The strict time-window filter (e.g. a 36h cutoff) is a completely separate concern from discovery breadth — widening *which sports* are queried must never widen the *time window* itself.
