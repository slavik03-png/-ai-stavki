"""
AI Betting Analytics -- a permanent, independent statistics module.

This package is completely separate from tracking/ and selection_engine/:
it owns its own SQLite database (data/analytics.db) and its own tables
(predictions, results, daily_statistics, market_statistics,
league_statistics, signal_statistics, monthly_statistics). It never
modifies the existing prediction model, the Telegram card rendering, the
daily archive, or football_cache's caching/quota logic -- it only reads
already-generated recommendations (via a thin hook in
ai_predictions/football_pipeline.py) and, separately, reads already-cached
fixture results through the existing FootballCache (never bypassing its
quota reserve).

Settlement re-uses tracking.settlement.settle_prediction (the same
deterministic engine the rest of the project uses) via a small adapter in
analytics/result_checker.py that translates this project's composite
market keys (e.g. "home_win", "over_2_5") into tracking's
(market_type, selection, line) vocabulary -- see MARKET_KEY_MAP in
analytics/config.py. Nothing here ever writes to tracking's own database
or changes prediction weights/scores (no self-learning).
"""
