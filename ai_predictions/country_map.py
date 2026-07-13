"""
Real, static country-name lookup derived from The Odds API's own sport_key
naming convention (e.g. "soccer_epl" -> England, "soccer_spain_la_liga" ->
Spain). This is metadata about which real competition a sport_key refers
to -- not a statistic, not invented -- used only to show "⚽ Страна" on the
concise Telegram card. Unknown/international competitions correctly return
None rather than a guessed country.
"""

from __future__ import annotations

from typing import Optional

#: sport_key -> Russian country/region display name. Keys are the exact
#: normalized (casefolded) sport_key The Odds API uses; this list is not
#: exhaustive, but every entry is a real, verifiable competition-country
#: pairing, never a guess. Unmapped keys simply show no country.
_COUNTRY_BY_SPORT_KEY = {
    "soccer_epl": "Англия",
    "soccer_england_league1": "Англия",
    "soccer_england_league2": "Англия",
    "soccer_england_efl_cup": "Англия",
    "soccer_fa_cup": "Англия",
    "soccer_spain_la_liga": "Испания",
    "soccer_spain_segunda_division": "Испания",
    "soccer_germany_bundesliga": "Германия",
    "soccer_germany_bundesliga2": "Германия",
    "soccer_italy_serie_a": "Италия",
    "soccer_italy_serie_b": "Италия",
    "soccer_france_ligue_one": "Франция",
    "soccer_france_ligue_two": "Франция",
    "soccer_netherlands_eredivisie": "Нидерланды",
    "soccer_portugal_primeira_liga": "Португалия",
    "soccer_belgium_first_div": "Бельгия",
    "soccer_switzerland_superleague": "Швейцария",
    "soccer_austria_bundesliga": "Австрия",
    "soccer_denmark_superliga": "Дания",
    "soccer_norway_eliteserien": "Норвегия",
    "soccer_sweden_allsvenskan": "Швеция",
    "soccer_poland_ekstraklasa": "Польша",
    "soccer_greece_super_league": "Греция",
    "soccer_turkey_super_league": "Турция",
    "soccer_russia_premier_league": "Россия",
    "soccer_japan_j_league": "Япония",
    "soccer_korea_kleague1": "Южная Корея",
    "soccer_china_superleague": "Китай",
    "soccer_australia_aleague": "Австралия",
    "soccer_usa_mls": "США",
    "soccer_mexico_ligamx": "Мексика",
    "soccer_brazil_campeonato": "Бразилия",
    "soccer_brazil_serie_b": "Бразилия",
    "soccer_argentina_primera_division": "Аргентина",
    "soccer_conmebol_copa_libertadores": "Южная Америка",
    "soccer_conmebol_copa_sudamericana": "Южная Америка",
    "soccer_uefa_champs_league": "Европа",
    "soccer_uefa_europa_league": "Европа",
    "soccer_uefa_europa_conference_league": "Европа",
    "soccer_uefa_european_championship": "Европа",
    "soccer_uefa_nations_league": "Европа",
    "soccer_fifa_world_cup": "Мир",
}


def country_for_sport_key(sport_key: Optional[str]) -> Optional[str]:
    if not sport_key:
        return None
    return _COUNTRY_BY_SPORT_KEY.get(sport_key.strip().lower())
