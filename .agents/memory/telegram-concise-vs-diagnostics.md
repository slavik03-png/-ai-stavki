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
