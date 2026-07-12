"""
AI predictions orchestration package -- connects real market data (The Odds
API) and real statistics (football/providers/api_football.py) into the
selection_engine pipeline and tracking storage, for the Telegram
"🤖 Прогнозы ИИ" button.

Deliberately isolated the same way tracking/ and selection_engine/ are:
bot.py only imports this package (never tracking/ or selection_engine/
directly), and this package never imports bot.py or telegram. It is the
one place allowed to depend on all three (football/, selection_engine/,
tracking/) plus the network, because orchestration has to happen
somewhere -- see tests/test_ai_predictions_isolation.py for the exact
boundary this enforces.

Scope of this first live version (documented so it is a visible choice,
not an accident):
- football only (tennis/hockey/basketball are out of scope; see
  football/interface.py and selection_engine/ which are already
  sport-agnostic enough to plug those in later without a redesign);
- markets are only ever built from what The Odds API actually returned in
  this run (h2h -> 1x2, totals -> total_goals, btts -> btts,
  team_totals -> team_total, double_chance -> double_chance). Asian
  handicap ("spreads") has no equivalent in selection_engine's market
  catalogue and is intentionally not mapped.
"""
