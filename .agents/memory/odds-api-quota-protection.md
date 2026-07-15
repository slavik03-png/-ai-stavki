---
name: Odds API quota protection & no-live-testing policy
description: Since 2026-07-15, real Odds API calls are strictly capped at one fetch per Yekaterinburg calendar day (via the existing daily archive), never triggered by /status, and only an admin may force a refresh. The agent must never make a real Odds API call for its own testing/verification without the user's explicit per-instance permission.
---

## Why this exists

The user explicitly asked for this after the agent's own live-data verification of the odds-first
pipeline (2026-07-15) burned that day's real Odds API quota, leaving the bot unable to fetch real
coefficients until the quota reset. The request had two parts:

1. **Testing policy (applies to the agent, not just the code):** never issue a real request to The
   Odds API for verification/testing purposes without the user's separate, explicit permission for
   that specific instance. All agent-side checks must use mock data or the existing persisted
   cache/archive — never `fetch_all_active_football_events`/`run_football_predictions` against the
   real API key just to "see if it works". This restriction is about the agent's own behavior, not
   something enforceable by a code change; re-affirm it before any future live-data check on this
   project.
2. **Runtime quota protection (implemented in code):** see below.

## What already existed vs. what was added

Most of the actual quota-protection mechanism (strict daily archive keyed to the Yekaterinburg
calendar day, `/status` being read-only, admin-only confirmed force-refresh) already existed in
`ai_predictions/football_pipeline.py` (`load_daily_archive`/`save_daily_archive`/
`is_refresh_in_progress`) and `bot.py` (`handle_ai_predictions`, `/refresh_data` +
`ADMIN_REFRESH_CONFIRM_PREFIX`) from an earlier iteration — it was not built from scratch this time.
What was added on 2026-07-15 was purely **visibility**: `FootballPipelineResult` gained
`odds_api_sports_queried` (real per-run call count), `odds_api_credits_remaining` (the
`x-requests-remaining` header from the last real call), and `odds_api_last_request_at` (UTC
timestamp of that call) — threaded through the daily-archive diagnostics dict and rendered in
`/status` alongside the existing API-Football quota lines, formatted in Yekaterinburg local time via
`ai_predictions.window.format_user_time` (never a raw UTC/ISO string).

**Why:** the runtime mechanics for "one real request per day, shared by everyone" were already
correct and tested; the gap was that an admin/user had no way to see exactly how much Odds API
quota had actually been spent, when, or whether the current numbers came from the archive or a
fresh call — which is precisely what matters after a quota-exhaustion incident.

**How to apply:** any future field that should show up in `/status` for either API (API-Football or
The Odds API) belongs in the daily-archive diagnostics dict (`save_daily_archive`'s `diagnostics =
{...}` and the parallel dict in `bot.py`'s `handle_ai_predictions`), never fetched live inside
`build_status_text` — that function must stay a pure read of persisted state so pressing "ℹ️
Статус" can never itself spend quota.
