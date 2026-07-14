---
name: Telegram card text must never reuse internal rationale/diagnostic strings
description: MarketCandidate.rationale (and similar internal explanation fields) are written for tracking/settlement/diagnostics and often name the data provider, quota state, or match counts -- never render them directly on a user-facing card.
---

The user-facing "🤖 Прогнозы ИИ" card must use its own short, non-technical
explanation generator (in ai_predictions/prediction_report.py:
`_short_explanation_ru`, built from `candidate.source` + signal level),
never `candidate.rationale` directly. `rationale` is intended for
tracking/settlement records (`_recommendation_to_prediction` in
football_pipeline.py) and for /status diagnostics, and can legitimately
mention API-Football, quota reserves, or match-count details that are
fine there but must not leak into a card a regular user reads.

**Why:** Prior formatting only interpolated `c.rationale` straight into
the card, so any future change to that internal string (e.g. adding more
diagnostic detail) would silently leak jargon into user-facing text.
Splitting the two keeps the presentation layer immune to internal-string
changes.

**How to apply:** Any new field or button that shows prediction reasoning
to the end user must go through its own dedicated
"basis sentence + optional caution sentence" builder keyed off structured
data (`source`, `sample_size_category`, `signal_level`), not off a free-text
internal explanation field. Also: cards/archive headers should use
`ai_predictions/window.format_card_time()` (Yekaterinburg, no city label)
rather than raw UTC or the older `format_display_time()` (which appends
"(Екатеринбург)" and is used elsewhere).
