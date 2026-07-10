# AI Ставки — Telegram Odds Bot

A personal Telegram bot that fetches live sports betting odds from [The Odds API](https://the-odds-api.com) and delivers them as CSV reports directly in chat.

## Stack

- **Python 3.11**
- **python-telegram-bot 21.6** — async Telegram bot framework
- **requests** — HTTP calls to The Odds API
- **pandas** — data formatting

## Entry point

```
python bot.py
```

## How to run

1. Add the required secrets (see below).
2. Press **Run** — the workflow starts `python bot.py`.
3. Open your Telegram bot and send `/start`.

## Required secrets

| Secret | Where to get it |
|--------|----------------|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) on Telegram |
| `ODDS_API_KEY` | [the-odds-api.com](https://the-odds-api.com) — free tier available |

## What the bot does

- `/start` — shows the main menu with sport buttons
- **⚽ Футбол / 🎾 Теннис / 🏒 Хоккей** — fetches odds for that sport and sends a CSV file
- **🎯 Получить всю линию** — fetches all sports at once
- **ℹ️ Статус** — checks whether both API keys are configured

## Sports covered

- Football: EPL, La Liga, Serie A, Bundesliga, Ligue 1, Champions League, Europa League
- Tennis: ATP, WTA
- Hockey: NHL, SHL, NL

## User preferences

- Keep existing project structure; do not migrate to a different bot library.
