"""
Persistent prediction tracking and results-statistics package.

Isolated from the football analytics engine (`football/`) and from
`bot.py`. It records every recommendation issued, settles it against real
event results once available, and maintains cumulative performance
statistics -- all in a SQLite database that survives restarts.

Nothing in this package is wired into `bot.py`; see `tracking.telegram_adapter`
for Telegram-ready text that a future, separately approved change can attach
to the bot.
"""
