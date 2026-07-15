---
name: Live in-play mode implementation patterns
description: Durable decisions for the Live (🔴) in-play prediction mode, reusable if the pattern needs replicating for another parallel mode.
---

- `tracking.models.RECOMMENDATION_GROUPS` is a pre-match-era enum (`main/alternative/high_risk/avoid`) with no "live" member. A new parallel mode must map into one of the existing groups (e.g. `"main"`) and rely solely on the dedicated `mode` field for its actual identity — never add an ad hoc new enum value just to name the mode.
  **Why:** `Prediction.__post_init__` validates `recommendation_group` against that fixed set; adding a bespoke value crashes at insert time.
  **How to apply:** when adding another mode alongside pre-match/live in `tracking/models.py` or `analytics/storage.py`, keep `recommendation_group` about risk tier, and thread the new mode only through the `mode` column/dedup key.
- A structurally different candidate type (e.g. `ValueCandidate`/`LiveFixture` vs. the pre-match `RankedRecommendation`/`Fixture`) can reuse a shared persistence function (`analytics.integration.record_recommendation`) via thin duck-typed adapter classes, without touching that function's contract.
  **Why:** avoids forking or parametrizing shared persistence code for every new candidate shape.
  **How to apply:** wrap the new type in a small adapter exposing only the attributes the shared function actually reads.
- A second cache/quota-consuming pipeline running alongside an existing one (e.g. Live next to the daily pre-match pool) should get its own cache key and its own in-process asyncio lock, but should still share the same underlying API quota reserve (`FootballCache`'s daily counter) rather than getting a separate allowance — the two must never spend budget as if they were unrelated services.
  **Why:** keeps the isolation guarantee (never blocks/corrupts the other flow) without silently doubling real API spend.
  **How to apply:** new cache key + own lock/globals in bot.py; reuse `cache.can_spend`/`record_requests` unchanged.
- SQLite test fixtures that reuse a fixed `/tmp/*.db` path across test runs accumulate rows from previous runs and can trip dedup/uniqueness assertions on a second local run (not just CI).
  **Why:** file persists between separate `pytest` invocations, unlike an in-memory DB.
  **How to apply:** always `os.remove(path)` if it exists before opening a fixed test DB path, or use a fresh tempfile per run.
