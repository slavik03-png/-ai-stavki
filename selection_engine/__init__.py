"""
AI Stavki selection & decision engine -- version 1.

Analyses every candidate prediction across all matches and markets, but
publishes only a small number of the strongest, verified recommendations
(normally at most 5 main picks). It never fills the output with weak
predictions just to reach a quota, and it never claims guaranteed profit.

This package is fully isolated:
- it does not import `bot.py` and is not imported by it;
- it does not perform any network requests;
- it reuses `tracking/` for persistence, settlement and historical
  statistics instead of creating a second, competing database structure;
- it reuses `football/` prediction output as the initial source of model
  probability, but does not modify the football engine.

Probability convention (documented once, used everywhere in this package):
    All probabilities (model_probability, bookmaker_implied_probability,
    fair-odds-derived probability, calibration inputs/outputs) are stored
    as floats in the 0..1 range, NOT 0..100. `confidence_score` is the one
    deliberate exception and is always 0..100, matching the existing
    `football` and `tracking` packages' convention for that field.

Submodules:
    models.py          -- CandidatePrediction dataclass and output groups
    config.py           -- SelectionConfig: thresholds, weights, market rules
    scoring.py          -- probability, edge, EV, completeness, confidence
    calibration.py      -- confidence calibration buckets
    filters.py           -- minimum-quality filters and rejection reasons
    diversification.py  -- correlation groups and anti-concentration rules
    selector.py          -- end-to-end pipeline: score -> filter -> rank -> group
    report.py            -- Russian daily report renderer
    versioning.py        -- explicit model/config/scoring/provider versions
"""
