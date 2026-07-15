---
name: Country display fallback for multi-country competitions
description: API-Football reports country="World" for continental club/national competitions; a real confederation-based region fallback (not a guess) improves this without fabricating per-match data.
---

Verified live via API-Football's `/leagues` endpoint (2026-07-15): UEFA Champions/Europa/Conference League, UEFA Nations League, CAF Champions League, AFC Champions League, and CONCACAF Champions League all genuinely report `country: "World"` — this is correct (they're multi-country), not a data bug.

**Why:** showing "Мир" (World) for every continental competition is technically accurate but unhelpful; the real confederation is already encoded in the competition's own verified name (e.g. "UEFA ...", "CAF ...", "AFC ...", "CONCACAF ...", "CONMEBOL Copa Libertadores/Sudamericana/Copa America") — mapping the confederation prefix to its real continent is not per-match fabrication, since it's a fixed fact about a named real organization.

**How to apply:** `ai_predictions.ru_translation.display_country_ru(league_country, league_name)` is the priority-ordered resolver: (1) real league_country if present and not "World", (2) confederation-name-based region fallback via `tournament_region_ru()`, (3) "Мир" only as genuine last resort (FIFA World Cup, most international Friendlies — these really have no single continent). Use this instead of the older `country_ru()` for any new country-display code in the football prediction cards.
