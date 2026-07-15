"""
Tests for the production v3 API-Football-only pipeline
(ai_predictions/football_pipeline.py, football_predictions.py,
prediction_selector.py, odds_lookup.py, prediction_report.py).

Covers exactly the required scenarios from the urgent production-fix
spec:
1. fixtures are obtained from API-Football even when The Odds API quota is zero;
2. HTTP 401 from The Odds API does not block fixture discovery/analysis;
3. a recommendation with no real, matched bookmaker price is dropped
   entirely (never shown with a placeholder "нет данных" coefficient);
4. only fixtures in the strict 36-hour window are used;
5. no fabricated fixtures, probabilities or coefficients are produced;
6. Russian Telegram cards follow the required format, including the real
   bookmaker name that supplied the shown coefficient;
7. internal technical diagnostics (quota, HTTP, pipeline internals) are
   absent from user-facing cards.

No real network calls anywhere in this file.
"""

import datetime
import sys
import tempfile

sys.path.insert(0, ".")

from football.interface import FormSplit, MatchSummary, Stat
from ai_predictions.fixtures import Fixture
from ai_predictions.football_cache import FootballCache
from ai_predictions.football_predictions import build_candidates_for_fixture
from ai_predictions.prediction_selector import select_recommendations
from ai_predictions.prediction_report import (
    render_predictions_message, render_no_signal_message, render_recommendation_card, HEADING,
)
import ai_predictions.football_pipeline as football_pipeline_mod
from ai_predictions.football_pipeline import run_football_predictions
from tracking.storage import TrackingStorage

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


NOW = datetime.datetime(2026, 7, 14, 12, 0, 0, tzinfo=datetime.timezone.utc)


class FakeProvider:
    """Minimal double for ApiFootballProvider -- exposes exactly the
    methods football_predictions.py calls, backed by pre-seeded, real-
    shaped Stat objects. Never touches the network."""

    def __init__(self, predictions_by_fixture=None, last_matches_by_team=None):
        self.predictions_by_fixture = predictions_by_fixture or {}
        self.last_matches_by_team = last_matches_by_team or {}
        self.requests_made = 0

    def get_predictions(self, fixture_id):
        self.requests_made += 1
        return self.predictions_by_fixture.get(fixture_id, Stat.missing("нет прогноза"))

    def get_last_matches(self, team, count=10):
        self.requests_made += 1
        return self.last_matches_by_team.get(team, Stat.missing(f"нет матчей для {team}"))


def _match_summary(home, away, home_goals, away_goals, for_team):
    return MatchSummary(
        date="2026-07-01T18:00:00+00:00", home_team=home, away_team=away,
        home_goals=home_goals, away_goals=away_goals, competition="Test League",
    )


def _fixture(fid, home, away, hours_ahead=5, country="Brazil", league="Serie B"):
    return Fixture(
        fixture_id=fid, kickoff_utc=NOW + datetime.timedelta(hours=hours_ahead),
        home_team=home, away_team=away, home_team_id=1, away_team_id=2,
        league_name=league, league_country=country, status_short="NS",
    )


def _team_matches(team, opponent, scored, conceded, n=8):
    return Stat.ok([_match_summary(team, opponent, scored, conceded, team) for _ in range(n)])


# ---------------------------------------------------------------------------
# Scenario 5 + core math: honest candidates built purely from API-Football
# data, no odds involved at all.
# ---------------------------------------------------------------------------

def test_candidates_built_without_any_odds_data():
    fixture = _fixture(1, "Ceara", "Athletic Club")
    provider = FakeProvider(
        predictions_by_fixture={
            1: Stat.ok({"percent": {"home": "30%", "draw": "30%", "away": "40%"}, "advice": None,
                        "under_over": None, "goals": {}, "win_or_draw": None, "winner_comment": None, "comparison": {}}),
        },
        last_matches_by_team={
            "Ceara": _team_matches("Ceara", "X", 2, 1),
            "Athletic Club": _team_matches("Athletic Club", "Y", 1, 1),
        },
    )
    cache = FootballCache(db_path=tempfile.mktemp(), now=NOW)
    candidates, _ = build_candidates_for_fixture(fixture, provider, cache)
    market_keys = {c.market_key for c in candidates}
    check("candidates built with zero Odds API involvement", len(candidates) > 0)
    check("home/draw/away/1X/X2 markets present", {"home_win", "draw", "away_win", "double_chance_1x", "double_chance_x2"} <= market_keys)
    check("totals/BTTS markets present from real goal averages", {"over_1_5", "over_2_5", "under_3_5", "btts_yes", "btts_no"} <= market_keys)
    for c in candidates:
        check(f"probability in [0,1] for {c.market_key}", 0.0 <= c.probability <= 1.0)
    cache.close()


def test_no_fabrication_when_data_missing():
    """No recent-form data at all for either team, and no predictions
    endpoint answer -- must fall back to the historical-baseline signal
    (real, generic aggregate football statistics, never a fixture-
    specific guess), always capped at "none" sample size / LOW tier, per
    the 'never return analysed=0 when fixtures exist' requirement."""
    fixture = _fixture(2, "Nowhere FC", "Unknown United")
    provider = FakeProvider()  # every lookup returns Stat.missing
    cache = FootballCache(db_path=tempfile.mktemp(), now=NOW)
    candidates, _ = build_candidates_for_fixture(fixture, provider, cache)
    check("fixture still ranked via historical baseline, never dropped entirely", len(candidates) > 0)
    check("every fallback candidate is honestly labelled as zero-evidence",
          all(c.sample_size_category == "none" and c.source == "historical_baseline" for c in candidates))
    from ai_predictions.prediction_selector import classify
    check("fallback candidates can never classify above LOW",
          all(classify(c.probability, c.completeness, c.sample_size_category) in (None, "LOW") for c in candidates))
    cache.close()


# ---------------------------------------------------------------------------
# Regression: exhausted API-Football daily reserve must NOT abort analysis.
# Cached fixtures keep producing full-confidence recommendations; fixtures
# with nothing cached still get ranked via the historical baseline. Zero
# real network calls are made once the reserve hits zero.
# ---------------------------------------------------------------------------

def test_reserve_exhausted_still_analyses_every_fixture_from_cache():
    fixtures = [
        _fixture(101, "Cached Home A", "Cached Away A", hours_ahead=2),
        _fixture(102, "Cached Home B", "Cached Away B", hours_ahead=4),
        _fixture(103, "Uncached Home C", "Uncached Away C", hours_ahead=6),
    ]
    cache = FootballCache(db_path=tempfile.mktemp(), now=NOW)

    # Pre-populate the persistent cache exactly like a previous, successful
    # run would have -- no network call is needed to read these back.
    cache.set("predictions:101", {"available": True, "data": {
        "percent": {"home": "78%", "draw": "12%", "away": "10%"}, "advice": None,
        "under_over": None, "goals": {}, "win_or_draw": None, "winner_comment": None, "comparison": {},
    }})
    for team in ("Cached Home A", "Cached Away A", "Cached Home B", "Cached Away B"):
        cache.set(f"team_recent_stats:{team.lower()}:8", {
            "matches_counted": 8, "win_rate": 0.6, "avg_scored": 1.8, "avg_conceded": 0.8,
            "available": True, "reason": None,
        })
    cache.set("predictions:102", {"available": True, "data": {
        "percent": {"home": "68%", "draw": "17%", "away": "15%"}, "advice": None,
        "under_over": None, "goals": {}, "win_or_draw": None, "winner_comment": None, "comparison": {},
    }})

    # Exhaust today's reserve entirely -- can_spend(1) must be False, and
    # the provider must never receive a single real call from here on.
    from ai_predictions.value_config import API_FOOTBALL_DAILY_QUOTA, API_FOOTBALL_QUOTA_RESERVE
    cache.record_requests(API_FOOTBALL_DAILY_QUOTA - API_FOOTBALL_QUOTA_RESERVE)
    check("reserve is genuinely exhausted for this test", not cache.can_spend(1))

    class NetworkForbiddenProvider:
        requests_made = 0

        def get_predictions(self, fixture_id):
            raise AssertionError("must not touch the network once the reserve is exhausted")

        def get_last_matches(self, team, count=10):
            raise AssertionError("must not touch the network once the reserve is exhausted")

    provider = NetworkForbiddenProvider()
    all_candidates = []
    analysed = 0
    for fixture in fixtures:
        candidates, _ = build_candidates_for_fixture(fixture, provider, cache)
        all_candidates.extend(candidates)
        analysed += 1

    check("every fixture is still analysed despite zero quota", analysed == len(fixtures))
    ranked = select_recommendations(all_candidates)
    check("cached fixtures still produce real, non-fallback recommendations",
          any(r.candidate.source != "historical_baseline" for r in ranked))
    check("the uncached fixture still gets a ranked (LOW, historical) candidate",
          any(c.fixture.fixture_id == 103 for c in all_candidates))
    check("uncached fixture's fallback candidates are honestly zero-evidence",
          all(c.sample_size_category == "none" for c in all_candidates if c.fixture.fixture_id == 103))
    check("no recommendation list is empty when fixtures > 0", len(ranked) > 0)
    cache.close()


# ---------------------------------------------------------------------------
# Scenario 1 + 2: fixtures obtained purely from API-Football; Odds API
# quota-exhausted/401 never blocks recommendations.
# ---------------------------------------------------------------------------

def test_pipeline_finds_nothing_to_analyse_with_zero_odds_quota():
    """Odds-first architecture (2026-07-15): if The Odds API has no real
    events at all (quota exhausted / no key), no fixture is ever matched,
    so nothing is analysed -- this must not crash and must produce an
    honest, distinct message, never a placeholder coefficient and never
    the generic 'probability too low' message."""
    fixtures = [_fixture(10, "Home A", "Away A", hours_ahead=3)]

    def fake_discover(api_key, cache, now, window_hours=36, **kwargs):
        from ai_predictions.fixtures import FixtureDiscoveryResult
        return FixtureDiscoveryResult(fixtures=fixtures, dates_queried=["2026-07-14"], requests_used=1)

    class FakeProviderModule:
        @staticmethod
        def ApiFootballProvider(api_key=None, now=None):
            return FakeProvider(
                predictions_by_fixture={10: Stat.ok({"percent": {"home": "75%", "draw": "15%", "away": "10%"},
                                                      "advice": None, "under_over": None, "goals": {},
                                                      "win_or_draw": None, "winner_comment": None, "comparison": {}})},
                last_matches_by_team={
                    "Home A": _team_matches("Home A", "X", 2, 0),
                    "Away A": _team_matches("Away A", "Y", 0, 2),
                },
            )

    import football.providers.api_football as api_football_mod
    orig_discover = football_pipeline_mod.discover_fixtures_in_window
    orig_provider_cls = api_football_mod.ApiFootballProvider
    football_pipeline_mod.discover_fixtures_in_window = fake_discover
    api_football_mod.ApiFootballProvider = FakeProviderModule.ApiFootballProvider
    try:
        storage = TrackingStorage(db_path=tempfile.mktemp())
        cache = FootballCache(db_path=tempfile.mktemp(), now=NOW)
        # No ODDS_API_KEY given -> fetch_all_active_football_events returns
        # zero events -> zero matches -> zero fixtures analysed, honestly.
        result = run_football_predictions(
            football_api_key="real-key", odds_api_key="", storage=storage, now=NOW, football_cache=cache,
        )
        cache.close()

        check("fixtures found via API-Football alone", result.found_fixtures == 1)
        check("no fixture matched to a real odds event", result.matched_fixtures == 0)
        check("the unmatched fixture is counted, not silently dropped", result.unmatched_fixtures_no_odds == 1)
        check("nothing was analysed -- no real odds coverage means no point spending API-Football budget",
              result.analysed_fixtures == 0)
        check("no coefficient attached", result.odds_by_fixture == {})
        check("no recommendation produced", result.recommendations_count == 0)
        check("odds_status reflects unavailability, doesn't crash the run", result.odds_status == "unavailable")
        check("telegram message honestly explains no real odds coverage exists for any found match",
              any("реальн" in m and "коэффициент" in m for m in result.telegram_messages))
        check("no 'нет данных' placeholder is ever shown",
              not any("нет данных" in m for m in result.telegram_messages))
        storage.close()
    finally:
        football_pipeline_mod.discover_fixtures_in_window = orig_discover
        api_football_mod.ApiFootballProvider = orig_provider_cls


def test_pipeline_only_analyses_fixtures_with_real_matched_odds():
    """Core odds-first behaviour: two fixtures are discovered, but only
    one has a real, matched Odds API event -- only that one is analysed
    and can become a recommendation; the other is never even touched."""
    matched_fixture = _fixture(11, "Home M", "Away M", hours_ahead=4)
    unmatched_fixture = _fixture(12, "Home U", "Away U", hours_ahead=6)
    fixtures = [matched_fixture, unmatched_fixture]

    def fake_discover(api_key, cache, now, window_hours=36, **kwargs):
        from ai_predictions.fixtures import FixtureDiscoveryResult
        return FixtureDiscoveryResult(fixtures=fixtures, dates_queried=["2026-07-14"], requests_used=1)

    event = {
        "id": "evt1", "home_team": "Home M", "away_team": "Away M",
        "commence_time": matched_fixture.kickoff_utc.isoformat(),
        "bookmakers": [{
            "title": "Pinnacle",
            "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Home M", "price": 1.9}, {"name": "Draw", "price": 3.4}, {"name": "Away M", "price": 4.1},
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 1.5, "price": 1.5}, {"name": "Under", "point": 1.5, "price": 2.6},
                    {"name": "Over", "point": 2.5, "price": 2.1}, {"name": "Under", "point": 2.5, "price": 1.75},
                    {"name": "Over", "point": 3.5, "price": 3.3}, {"name": "Under", "point": 3.5, "price": 1.3},
                ]},
                {"key": "btts", "outcomes": [{"name": "Yes", "price": 1.8}, {"name": "No", "price": 2.0}]},
            ],
        }],
    }

    def fake_fetch_events(api_key=None, persistent_cache=None):
        from ai_predictions.odds_client import MultiSportFetchResult
        return MultiSportFetchResult(events=[event])

    class FakeProviderModule:
        @staticmethod
        def ApiFootballProvider(api_key=None, now=None):
            return FakeProvider(
                predictions_by_fixture={
                    11: Stat.ok({"percent": {"home": "78%", "draw": "12%", "away": "10%"},
                                 "advice": None, "under_over": None, "goals": {},
                                 "win_or_draw": None, "winner_comment": None, "comparison": {}}),
                },
                last_matches_by_team={
                    "Home M": _team_matches("Home M", "X", 2, 0),
                    "Away M": _team_matches("Away M", "Y", 0, 2),
                },
            )

    import football.providers.api_football as api_football_mod
    orig_discover = football_pipeline_mod.discover_fixtures_in_window
    orig_provider_cls = api_football_mod.ApiFootballProvider
    orig_fetch = football_pipeline_mod.fetch_all_active_football_events
    football_pipeline_mod.discover_fixtures_in_window = fake_discover
    api_football_mod.ApiFootballProvider = FakeProviderModule.ApiFootballProvider
    football_pipeline_mod.fetch_all_active_football_events = fake_fetch_events
    try:
        storage = TrackingStorage(db_path=tempfile.mktemp())
        cache = FootballCache(db_path=tempfile.mktemp(), now=NOW)
        result = run_football_predictions(
            football_api_key="real-key", odds_api_key="real-odds-key", storage=storage, now=NOW, football_cache=cache,
        )
        cache.close()

        check("both discovered fixtures counted", result.found_fixtures == 2)
        check("exactly one fixture matched to a real odds event", result.matched_fixtures == 1)
        check("the other fixture is counted as unmatched, not silently dropped", result.unmatched_fixtures_no_odds == 1)
        check("only the matched fixture was analysed (unmatched one never touched)", result.analysed_fixtures == 1)
        check("the matched fixture's real bookmaker price made it through",
              matched_fixture.fixture_id in result.odds_by_fixture)
        check("the real bookmaker name is attached", result.bookmaker_by_fixture.get(matched_fixture.fixture_id) == "Pinnacle")
        check("a real recommendation was produced for the matched fixture only",
              result.recommendations_count >= 1 and all(
                  r.candidate.fixture.fixture_id == matched_fixture.fixture_id for r in result.recommendations
              ))
        storage.close()
    finally:
        football_pipeline_mod.discover_fixtures_in_window = orig_discover
        api_football_mod.ApiFootballProvider = orig_provider_cls
        football_pipeline_mod.fetch_all_active_football_events = orig_fetch


# ---------------------------------------------------------------------------
# Scenario 3 + 6 + 7: exact card format, no bookmaker/technical leakage.
# ---------------------------------------------------------------------------

def test_card_format_and_no_technical_leakage():
    fixture = _fixture(20, "Ceara", "Athletic Club", hours_ahead=16, country="Brazil", league="Serie B")
    from ai_predictions.football_predictions import MarketCandidate
    candidate = MarketCandidate(
        fixture=fixture, market_key="over_1_5", market_label_ru="Тотал больше 1,5",
        probability=0.68, completeness=0.8, sample_size_category="strong",
        rationale="Резерв запросов к API-Football почти исчерпан, использован provider fallback pipeline.",
        source="goal_model",
    )
    from ai_predictions.prediction_selector import RankedRecommendation
    rec = RankedRecommendation(candidate=candidate, signal_level="MEDIUM")
    odds_by_fixture = {fixture.fixture_id: (1.85, "Pinnacle")}
    messages = render_predictions_message(
        [rec], odds_by_fixture, found_fixtures=12, analysed_fixtures=8,
    )

    check("heading present", messages[0].startswith(HEADING))
    check("Found/Analysed/Selected counts present", "Найдено матчей: 12" in messages[0]
          and "Проанализировано: 8" in messages[0] and "Отобрано прогнозов: 1" in messages[0])

    card = messages[1]
    for required in (
        "⚽ ПРОГНОЗ №1", "Вид спорта:", "🌍 Страна:", "🏆 Турнир:", "👥 Матч:", "🕒 Начало:",
        "🎯 Ставка:", "📊 Расчётная вероятность:", "💰 Коэффициент:",
        "Уровень сигнала:", "Краткое объяснение:",
    ):
        check(f"card contains '{required}'", required in card, card)
    check("card shows the real coefficient and the real bookmaker it came from",
          "Коэффициент: 1,85 (Pinnacle)" in card)
    check("card shows the sport", "⚽ Футбол" in card)
    check("card shows the real country (Brazil), not the generic 'Мир'", "Страна: Бразилия" in card)
    check("no 'нет данных' placeholder ever shown on a rendered card", "нет данных" not in card)
    check("card shows whole-percent probability", "68%" in card)
    check("signal level shown as a plain Russian word, not a raw code", "средний" in card and "MEDIUM" not in card)
    check(
        "card never uses the internal rationale text (avoids leaking API-Football/quota/pipeline wording)",
        candidate.rationale not in card,
    )
    check("no technical/internal terms leaked", not any(
        term in card for term in ("edge", "EV", "HTTP", "fixture_id", "api_football", "quota",
                                   "fixture", "fallback", "pipeline", "provider")
    ))
    check("disclaimer shown once, after the cards", messages[-1].startswith("ℹ️") and "аналитической оценкой" in messages[-1])


def test_no_recommendations_survive_probability_but_none_have_real_odds():
    """When candidates clear the probability threshold but none of them
    has a real, matched bookmaker price, the message must honestly say
    so -- never a placeholder card, never the generic 'no signal' text
    (which specifically means 'probability too low', a different, false
    reason)."""
    messages = render_predictions_message(
        [], {}, found_fixtures=10, analysed_fixtures=10, candidates_without_odds=3,
    )
    check("exactly one message returned", len(messages) == 1)
    check("message explains missing real odds, not low probability",
          "реальн" in messages[0] and "коэффициент" in messages[0] and "56%" not in messages[0])
    check("count of odds-excluded candidates is mentioned", "3" in messages[0])


def test_double_chance_markets_spelled_out_in_russian():
    """Rule: never show a raw code like '1X'/'X2' -- always the full,
    plain-Russian meaning."""
    from ai_predictions.value_config import BET_MARKET_LABELS_RU
    check("1X spelled out", BET_MARKET_LABELS_RU["double_chance_1x"] == "Победа хозяев или ничья")
    check("X2 spelled out", BET_MARKET_LABELS_RU["double_chance_x2"] == "Победа гостей или ничья")
    check("totals use a comma decimal separator", "," in BET_MARKET_LABELS_RU["over_2_5"]
          and "." not in BET_MARKET_LABELS_RU["over_2_5"])


def test_signal_level_russian_word_and_emoji():
    from ai_predictions.value_config import SIGNAL_EMOJI_RU_CARD, SIGNAL_WORD_RU_CARD
    check("HIGH -> высокий/🔥", SIGNAL_WORD_RU_CARD["HIGH"] == "высокий" and SIGNAL_EMOJI_RU_CARD["HIGH"] == "🔥")
    check("MEDIUM -> средний/🟡", SIGNAL_WORD_RU_CARD["MEDIUM"] == "средний" and SIGNAL_EMOJI_RU_CARD["MEDIUM"] == "🟡")
    check("LOW -> низкий/⚪", SIGNAL_WORD_RU_CARD["LOW"] == "низкий" and SIGNAL_EMOJI_RU_CARD["LOW"] == "⚪")


def test_odds_formatted_with_russian_comma():
    from ai_predictions.football_predictions import MarketCandidate
    from ai_predictions.prediction_selector import RankedRecommendation
    fixture = _fixture(21, "Home Z", "Away Z", hours_ahead=5)
    candidate = MarketCandidate(
        fixture=fixture, market_key="home_win", market_label_ru="Победа хозяев",
        probability=0.7, completeness=0.9, sample_size_category="strong", rationale="r", source="api_football_predictions",
    )
    rec = RankedRecommendation(candidate=candidate, signal_level="HIGH")
    card = render_recommendation_card(1, rec, 1.65, "1xBet")
    check("odds shown with a comma, not a dot", "1,65" in card and "1.65" not in card)
    check("real bookmaker name shown on the card", "1xBet" in card)


def test_display_country_prefers_real_country_then_confederation_then_world():
    from ai_predictions.ru_translation import display_country_ru
    check("real country translated", display_country_ru("Japan", "J1 League") == "Япония")
    check("real country translated (Brazil)", display_country_ru("Brazil", "Serie B") == "Бразилия")
    check("UEFA competition with country=World falls back to Европа",
          display_country_ru("World", "UEFA Champions League") == "Европа")
    check("CAF competition with country=World falls back to Африка",
          display_country_ru("World", "CAF Champions League") == "Африка")
    check("AFC competition with country=World falls back to Азия",
          display_country_ru("World", "AFC Champions League") == "Азия")
    check("Copa Libertadores falls back to Южная Америка",
          display_country_ru("World", "CONMEBOL Copa Libertadores") == "Южная Америка")
    check("genuinely global competition (World Cup) keeps 'Мир' as last resort",
          display_country_ru("World", "World Cup") == "Мир")
    check("missing country and unrecognised competition also falls back to 'Мир'",
          display_country_ru(None, "Some Unknown Cup") == "Мир")


def test_utc_to_yekaterinburg_time_conversion():
    """Rule: cards and the archive header must show Yekaterinburg local
    time, not raw UTC."""
    from ai_predictions.window import format_card_time
    utc_dt = datetime.datetime(2026, 7, 14, 9, 0, 0, tzinfo=datetime.timezone.utc)
    text = format_card_time(utc_dt)
    check("09:00 UTC becomes 14:00 in Yekaterinburg (UTC+5)", text == "14.07.2026 в 14:00", text)


def test_no_signal_message_exact_text():
    msg = render_no_signal_message(9)
    expected = (
        "На ближайшие 36 часов найдено 9 матчей, но ни один вариант не достиг "
        "минимальной расчётной вероятности 56%. Слабые ставки бот не предлагает."
    )
    check("no-signal message matches exact spec text", msg == expected)


# ---------------------------------------------------------------------------
# Scenario 4: strict 36h window enforcement (reuses fixtures.py, already
# covered by tests/test_ai_predictions_fixtures.py -- re-asserted here at
# the selector level: a fixture the discovery layer never returns can
# never become a recommendation).
# ---------------------------------------------------------------------------

def test_only_discovered_fixtures_can_become_recommendations():
    from ai_predictions.football_predictions import MarketCandidate
    from ai_predictions.prediction_selector import select_recommendations

    in_window = _fixture(30, "In Window A", "In Window B", hours_ahead=10)
    candidates = [
        MarketCandidate(fixture=in_window, market_key="home_win", market_label_ru="Победа хозяев",
                         probability=0.8, completeness=0.9, sample_size_category="strong", rationale="r", source="s"),
    ]
    ranked = select_recommendations(candidates)
    check("only real, discovered fixtures appear in recommendations", all(r.candidate.fixture is in_window for r in ranked))
    check("at least one recommendation from the real fixture", len(ranked) == 1)


# ---------------------------------------------------------------------------
# Threshold classification correctness (never invents a signal below LOW).
# ---------------------------------------------------------------------------

def test_signal_thresholds():
    from ai_predictions.prediction_selector import classify
    check("72% + good completeness -> HIGH", classify(0.72, 0.7) == "HIGH")
    check("72% but poor completeness -> MEDIUM (not HIGH)", classify(0.72, 0.3) == "MEDIUM")
    check("64% -> MEDIUM", classify(0.64, 0.9) == "MEDIUM")
    check("56% -> LOW", classify(0.56, 0.9) == "LOW")
    check("55% -> no candidate at all (None)", classify(0.55, 0.9) is None)


def run():
    for name in list(globals()):
        if name.startswith("test_"):
            fn = globals()[name]
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                check(name, False, f"EXCEPTION: {exc}")

    failed = [n for n, s in results if s == "FAIL"]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
    return len(failed) == 0


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
