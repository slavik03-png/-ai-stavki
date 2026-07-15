---
name: Request-time re-selection from a persisted pool
description: The daily archive stores the FULL ranked, odds-backed candidate pool, not a fixed top-5 message; every request re-filters it against "now" (kickoff + lead-time buffer) before rendering/persisting.
---

Replaying a fixed top-5 message computed once per day breaks as soon as
those fixtures start — a user could be shown a match already in play.
Fixed since 2026-07-15: `run_football_predictions` ranks and prices the
FULL candidate pool (never sliced), and the daily archive persists that
whole pool (`DailyArchive.pool`, JSON-serialized fixtures/candidates) —
not a rendered message list.

Every request — the first of the day and every later button press —
calls the same re-selection path against the CURRENT moment: drop any
fixture that has started, finished, or starts within
`MIN_LEAD_TIME_MINUTES` (30 min, `value_config.MIN_LEAD_TIME_MINUTES`),
then take the best up-to-5 of whatever real candidates remain
(`prediction_selector.select_current_recommendations`). No network call
is ever made for this — it's pure CPU over the already-fetched pool.

**Why:** this is what makes "morning picks auto-replaced by evening once
they've started" and "never show a match that already kicked off"
possible without spending additional API-Football/Odds API quota.

**How to apply:** never render or persist a recommendation straight from
`FootballPipelineResult.recommendations`/`telegram_messages` on a
*later* request — those are only the pipeline run's own initial
selection. A later request must go through
`football_pipeline.reselect_from_archive` (bot.py never imports
`tracking`/`analytics` directly — that function owns those internally so
the tracking/bot isolation tests stay green). Persisting a selection
reuses `TrackingStorage`'s existing dedup key (event+market+selection+
model_version, no date component), so re-selecting the same still-valid
pick across requests is a safe no-op, and a newly-surfaced pick (backup
that only fit once a slot opened up) gets saved for the first time.
Selection is never padded below the cap with a weaker/fake candidate —
if fewer real candidates remain after the time filter, fewer are shown.
