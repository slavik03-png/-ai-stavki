---
name: Display selection vs canonical settlement token
description: ValueCandidate.selection is a human display string, but tracking/settlement.py expects canonical lowercase tokens — do not conflate the two.
---

`ai_predictions/value_engine.py`'s `ValueCandidate.selection` is built by
`_display_selection()` as a human-facing string (a real team name, "Ничья",
or the literal English "Over"/"Under" for totals). That exact string is
stored verbatim into `Prediction.selection` and persisted to tracking.

Meanwhile `tracking/settlement.py`'s per-market settlers (`_settle_1x2`,
`_settle_total_goals`, etc.) compare against canonical lowercase tokens
("home"/"away"/"draw"/"over"/"under") when checking `p.selection == actual`.

**Why:** this is a latent mismatch between what gets stored and what
settlement expects to read back. It predates any Telegram-formatting work
and was deliberately left untouched during the Russian-language concise
message effort, since fixing it is a settlement/storage change, not a
presentation change.

**How to apply:** any new Russian/human translation of a selection (e.g.
"Тотал больше 2,5") must happen only in a rendering/display helper, and must
never mutate `candidate.selection` itself or the value written to
`Prediction.selection`. If this mismatch is ever fixed, it needs to be done
in `value_engine.py`/`settlement.py` together, with existing tracked
predictions considered, not as a side effect of a display change.
