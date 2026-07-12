---
name: Transient error cache poisoning
description: A per-run cache that stores "not found" on any error (including rate limits/network errors) turns a temporary failure into a permanent false negative for the rest of the run.
---

Pattern seen in a football-stats provider: `_resolve_team_id`/similar helpers cached `None` (meaning
"unresolvable") whenever the underlying HTTP call returned ANY error — including transient ones like HTTP 429
(rate limited) or network exceptions. Once poisoned, every later call for that key in the same run short-circuited
to "not found", masking the real (transient) cause and starving all dependent data fields.

**Why it matters:** this silently degrades a run from "temporarily degraded" to "permanently broken for this
key", and the resulting downstream symptom (e.g. 0% data completeness) looks like a filter/config problem rather
than a caching bug — easy to misdiagnose.

**How to apply:** only cache a negative/"not found" result when the API gave a genuine, confirmed empty answer
(e.g. an empty search response list). Never cache negative results for transient failures (network errors, HTTP
429/5xx, exhausted budget) — return the error for that call without writing it to the cache, so a later retry in
the same run can still succeed.
