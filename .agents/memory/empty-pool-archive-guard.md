---
name: Empty pool archive guard
description: How and why an archive with pool=[] is treated as invalid, preventing "all shown/started" from appearing when no picks ever existed.
---

## Rule

`is_archive_valid(archive)` is the ONLY correct way to check whether today's pool has real predictions. It requires `archive is not None AND len(archive.pool) > 0`. The old `archive is not None` check was wrong — a quota-exhausted run saves an empty archive, and `archive is not None` was True, leading to `render_nothing_left_for_user_message()` being shown even though no picks ever existed.

## Why

When Odds API quota is exhausted the pipeline runs, finds no odds-backed candidates, saves an archive with `pool=[]`. Every subsequent button press loaded this archive and, because `len(selected_entries)==0`, returned `render_nothing_left_for_user_message()` — "все варианты уже были показаны или матчи начались". This is a lie: no variants ever existed in today's pool.

## How to apply

- `handle_ai_predictions`: replace `if archive is not None` with `if is_archive_valid(archive)`. When `archive is not None and not is_archive_valid(archive)` → call `_reply_empty_pool()`, not `_reply_from_pool()`.
- `save_daily_archive`: if new result has empty pool AND existing valid archive is present → skip the write (guard added in `save_daily_archive`).
- `LAST_SUCCESSFUL_ARCHIVE_KEY`: updated every time a non-empty pool is saved. `load_last_successful_archive()` reads it regardless of calendar day, used as fallback by `_reply_empty_pool`.
- `ODDS_CREDITS_CACHE_KEY`: set after every real Odds API call. Pipeline skips the live call when this cache says "0".
- `/status` (`build_status_text`): when `pool_total==0` and archive exists → show "не сформирован" + `archive_empty_reason(archive_diagnostics)`, NOT "актуален" or "Источник: архив текущего дня".
- New message: `render_pool_empty_message(reason)` for empty-pool case; `render_nothing_left_for_user_message()` only for valid-pool-but-all-shown/started case.
