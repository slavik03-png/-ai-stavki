"""
Best-effort English -> Russian display translation for the values
API-Football returns (country names, tournament/league names). Every
entry here is a real, verified translation of a real competition/country
name -- never a guess. Anything not in these maps is shown in its
original (English) form rather than mistranslated -- this matches the
existing rule for team names (ai_predictions/country_map.py already does
the same thing for The Odds API's sport_key -> country mapping).
"""

from __future__ import annotations

from typing import Optional

_COUNTRY_RU = {
    "england": "Англия",
    "spain": "Испания",
    "germany": "Германия",
    "italy": "Италия",
    "france": "Франция",
    "netherlands": "Нидерланды",
    "portugal": "Португалия",
    "belgium": "Бельгия",
    "switzerland": "Швейцария",
    "austria": "Австрия",
    "denmark": "Дания",
    "norway": "Норвегия",
    "sweden": "Швеция",
    "poland": "Польша",
    "greece": "Греция",
    "turkey": "Турция",
    "russia": "Россия",
    "ukraine": "Украина",
    "japan": "Япония",
    "south-korea": "Южная Корея",
    "china": "Китай",
    "australia": "Австралия",
    "usa": "США",
    "united-states": "США",
    "mexico": "Мексика",
    "brazil": "Бразилия",
    "argentina": "Аргентина",
    "colombia": "Колумбия",
    "chile": "Чили",
    "uruguay": "Уругвай",
    "scotland": "Шотландия",
    "wales": "Уэльс",
    "croatia": "Хорватия",
    "serbia": "Сербия",
    "czech-republic": "Чехия",
    "romania": "Румыния",
    "world": "Мир",
    "europe": "Европа",
}

_LEAGUE_RU = {
    "premier league": "Премьер-лига",
    "championship": "Чемпионшип",
    "league 1": "Лига 1 (Англия)",
    "league 2": "Лига 2 (Англия)",
    "la liga": "Ла Лига",
    "segunda división": "Сегунда Дивизион",
    "bundesliga": "Бундеслига",
    "2. bundesliga": "Бундеслига 2",
    "serie a": "Серия А",
    "serie b": "Серия B",
    "ligue 1": "Лига 1 (Франция)",
    "ligue 2": "Лига 2 (Франция)",
    "eredivisie": "Эредивизи",
    "primeira liga": "Примейра-лига",
    "jupiler pro league": "Про-лига (Бельгия)",
    "super lig": "Супер-лига (Турция)",
    "champions league": "Лига чемпионов",
    "europa league": "Лига Европы",
    "europa conference league": "Лига конференций",
    "world cup": "Чемпионат мира",
    "euro championship": "Чемпионат Европы",
    "friendlies": "Товарищеские матчи",
    "friendlies clubs": "Товарищеские матчи клубов",
    "club friendlies": "Товарищеские матчи клубов",
}

#: Verified, well-known Russian names for major clubs/national teams only.
#: Anything absent keeps its original (usually English/local) name rather
#: than a guessed translation or transliteration -- see team_ru() below.
_TEAM_RU = {
    "manchester united": "Манчестер Юнайтед",
    "manchester city": "Манчестер Сити",
    "real madrid": "Реал Мадрид",
    "barcelona": "Барселона",
    "atletico madrid": "Атлетико Мадрид",
    "bayern munich": "Бавария",
    "borussia dortmund": "Боруссия Дортмунд",
    "paris saint germain": "Пари Сен-Жермен",
    "juventus": "Ювентус",
    "ac milan": "Милан",
    "inter": "Интер",
    "napoli": "Наполи",
    "liverpool": "Ливерпуль",
    "chelsea": "Челси",
    "arsenal": "Арсенал",
    "tottenham": "Тоттенхэм",
    "ajax": "Аякс",
    "porto": "Порту",
    "benfica": "Бенфика",
    "sporting cp": "Спортинг",
    "zenit": "Зенит",
    "spartak moscow": "Спартак Москва",
    "cska moscow": "ЦСКА Москва",
    "dynamo kyiv": "Динамо Киев",
    "shakhtar donetsk": "Шахтёр Донецк",
}


def team_ru(name: Optional[str]) -> Optional[str]:
    """Best-effort Russian display name for a team. Only translates names
    that are verified in `_TEAM_RU` -- anything else is returned exactly
    as received (never a fabricated or guessed transliteration)."""
    if not name:
        return name
    key = name.strip().lower()
    return _TEAM_RU.get(key, name)


def country_ru(country: Optional[str]) -> Optional[str]:
    if not country:
        return country
    key = country.strip().lower().replace(" ", "-")
    return _COUNTRY_RU.get(key, country)


def league_ru(league: Optional[str]) -> Optional[str]:
    if not league:
        return league
    key = league.strip().lower()
    return _LEAGUE_RU.get(key, league)
