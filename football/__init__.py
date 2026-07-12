"""
Football statistics subsystem.

This package is intentionally isolated from bot.py: the Telegram bot, its
menus, caching, and Odds API integration are untouched. It exists so a
future "full football match analysis" feature can plug in a statistics
provider without any rewrite of the bot itself.

Structure:
- football.interface     -- Stat wrapper + data shapes + the
                             FootballStatisticsProvider abstract base class
                             every provider must implement.
- football.providers.mock_provider
                          -- MockFootballProvider: sample data only, used to
                             test the architecture end-to-end.
- football.providers.api_football
                          -- ApiFootballProvider: structural template for a
                             real API-Football integration. Makes no network
                             calls and requires no secret yet.
- football.report         -- provider-agnostic Russian-language report
                             renderer that works against any provider.
"""
