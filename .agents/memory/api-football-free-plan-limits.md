---
name: API-Football free plan limits
description: What the API-Football free plan actually blocks and how to handle each restriction without surfacing errors to the user.
---

## Restrictions (confirmed against live API)

1. **`last` / `next` fixture params** — only work for seasons within `API_FOOTBALL_FREE_PLAN_SEASONS = {2022, 2023, 2024}`. Since July 2026, `_season_for(now)` returns 2026, which is out of range. Fix: always send `season=min(self.season, FREE_PLAN_MAX_SEASON)` in `_fetch_fixtures` — this constrains team-history lookups to the most recent allowed season (2024) instead of crashing.

2. **`date` param** — restricted to a 3-day window: [today − 1 day, today + 1 day] UTC. The 36-hour analysis window can include dates 2 days ahead, which the free plan rejects. Fix: in `discover_fixtures_in_window` (fixtures.py), clamp the date list to `max_date = today_utc + timedelta(days=API_FOOTBALL_FREE_PLAN_DATE_AHEAD_DAYS)` BEFORE any network call, so the error "Free plans do not have access to this date" is never reached.

**Why:** Attempting a request the free plan will reject wastes a quota unit AND surfaces a confusing error. Pre-filtering silently skips the date; the fixture-window filter then rejects any fixtures whose kickoff falls outside the analysis window anyway, so nothing real is missed.

**How to apply:** Any new endpoint that takes a `date`, `season`, `last`, or `next` param: check `API_FOOTBALL_FREE_PLAN_SEASONS` / `API_FOOTBALL_FREE_PLAN_DATE_AHEAD_DAYS` before constructing the request. Both constants live in `ai_predictions/value_config.py`.
