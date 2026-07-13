---
name: Telegram output — concise vs diagnostics split
description: Keep the user-facing prediction message and technical diagnostics as two separate render functions/paths; never merge them.
---

The bot's ranked-signal feature has two distinct audiences for two distinct renders:

- A concise, Russian, non-technical message (numbered signal cards + a one-line
  HIGH/MEDIUM/LOW/rejected count summary) sent to the end user on the
  "🤖 Прогнозы ИИ" button.
- A full diagnostics report (discovery/query counts, validation/dedup counts,
  per-competition skip reasons, HTTP error lists, rejection-reason frequency)
  reserved for "ℹ️ Статус" / `/status`.

**Why:** the two audiences have opposite needs — the end user wants a short,
readable, jargon-free result; debugging/ops needs the raw detail. Collapsing
them back into one message (as the original implementation did by sending the
full diagnostics report directly to the user) makes the bot output
unreadable and leaks internal API/HTTP detail to a non-technical audience.

**How to apply:** any new pipeline output field should be classified as
"shown to the user" or "diagnostics only" before it's added to either render
path. The diagnostics object should be cached separately (not just inside the
30-minute prediction cache) so `/status` can report the last real run's
diagnostics even after the prediction cache itself expires.

**Relabeling one surface without touching the other:** when a requirement applies to only one of the two
renders (e.g. "the user-facing card must show Russian signal levels"), add a *new*, separate label
dict/constant for that render instead of mutating the shared constant both renders read from. Existing tests
often assert exact literals (e.g. "🔥 HIGH" substrings, an exact "Итого: HIGH — n, ..." line) — grep the test
file for every literal before changing a shared label constant, since a blind find-replace silently breaks the
render you didn't mean to touch.
