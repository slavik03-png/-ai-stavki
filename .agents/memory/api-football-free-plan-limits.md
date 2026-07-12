---
name: API-Football free plan limits
description: Why a configured FOOTBALL_API_KEY can return zero real statistics even though requests succeed with HTTP 200.
---

The API-Football (v3.football.api-sports.io) free plan tier rejects, inside a 200 response's `errors` field
(not via HTTP status), two things relevant to any "current match" prediction feature:
- The `last`/`next` convenience params on `/fixtures` ("Free plans do not have access to the Last/Next parameter").
- Any season outside 2022-2024 ("Free plans do not have access to this season, try from 2022 to 2024").

**Why it matters:** this means a free-tier key can never supply real team-form/H2H/stats data for the current
season, no matter how the provider code is written — every such call comes back as a legitimate "no data",
not a bug. Do not spend time "fixing" candidate-builder or selection-engine logic before checking the raw
`/fixtures` response `errors` field for this plan message.

**How to apply:** if a football-stats-dependent feature returns systematically empty data for real API calls,
curl `/fixtures?team=<id>&season=<current_year>` directly and read `errors` before assuming a code bug. Fixing
requires an upgraded API-Football plan (or restructuring around a different real signal, e.g. cross-bookmaker
price-dispersion value betting instead of team-strength stats) — it is a plan/product decision, not a patch.
