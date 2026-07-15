---
name: Per-user shown-tracking design
description: How per-Telegram-user "already shown" exclusion was layered onto the shared daily pool without breaking non-per-user callers.
---

The shared daily pool (see request-time-reselection.md) now supports per-user exclusion via an
`exclude_keys: Optional[Set[Tuple[fixture_id, market_key]]]` parameter threaded through
`select_and_render`/`reselect_from_archive`/`run_football_predictions`.

**Why `None` vs an empty set matters:** passing `None` means "this caller doesn't do per-user
tracking at all" and preserves the exact prior behaviour/messages (old generic no-signal
templates). Passing any set (even empty) marks the call as user-scoped: an empty selection result
then returns the exact required "nothing left in today's pool for you" message instead of the
generic templates, which would describe the original run and mislead a user who has simply seen
everything already.

**How to apply:** any new call site that re-selects from the pool on behalf of a specific Telegram
user must pass both `football_cache` and `telegram_user_id` together (never just one) — that pair
is the single signal used everywhere to decide "is this user-scoped or legacy". The per-user
shown-history itself lives in a `shown_picks` table inside the same football_cache SQLite db,
keyed by (local Yekaterinburg calendar date, telegram_user_id, fixture_id, market_key) — separate
from the shared pool/archive entry, so clearing one user's history never touches the pool, tracking
or analytics.
