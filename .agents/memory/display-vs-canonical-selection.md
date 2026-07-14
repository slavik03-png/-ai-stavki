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

**Related, separate mismatch:** `MarketCandidate.market_key` (the
fixture-discovery-first pipeline's composite key, e.g. `"home_win"`,
`"over_2_5"`, `"double_chance_1x"`, `"btts_yes"`) is stored verbatim into
`Prediction.market_type` by `football_pipeline.py`'s
`_recommendation_to_prediction`, but `tracking/settlement.py`'s `_SETTLERS`
dict keys on canonical types (`"1x2"`, `"double_chance"`, `"total_goals"`,
`"btts"`) with separate `selection`/`line` fields — so `market_type` lookup
fails and these tracking rows can never actually be settled by
`settle_prediction` as stored. This is a second, pre-existing latent bug in
the same family as the one above (out of scope to fix in `tracking`/
`ai_predictions` directly). The `analytics/` module (permanent stats DB,
independent of `tracking/`) works around it locally with its own
`MARKET_KEY_MAP` (`analytics/config.py`) that translates the 10 composite
keys into `(market_type, selection, line)` triples before calling
`settle_prediction` — that translation is analytics-only adapter code, not
a fix to the underlying `tracking`/`ai_predictions` mismatch.
