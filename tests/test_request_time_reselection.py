"""
Covers the 2026-07-15 request-time re-selection redesign
(ai_predictions/prediction_selector.py + football_pipeline.py): every
request re-filters the persisted daily pool against the CURRENT moment
instead of replaying a fixed top-5, so already-started/imminent fixtures
are dropped and, when real candidates remain, fresh ones automatically
take their place -- without any network call. No real API calls happen
in this file at all.
"""

import datetime
import sys
import tempfile

sys.path.insert(0, ".")

from ai_predictions.fixtures import Fixture
from ai_predictions.football_pipeline import (
    FootballPipelineResult,
    load_daily_archive,
    reselect_from_archive,
    save_daily_archive,
)
from ai_predictions.football_cache import FootballCache
from ai_predictions.football_predictions import MarketCandidate
from ai_predictions.prediction_selector import (
    MAX_RECOMMENDATIONS,
    MIN_LEAD_TIME_MINUTES,
    RankedRecommendation,
    has_enough_lead_time,
    rank_all_candidates,
    select_current_recommendations,
)
from analytics.storage import AnalyticsStorage
from tracking.storage import TrackingStorage

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


NOW = datetime.datetime(2026, 7, 15, 12, 0, tzinfo=datetime.timezone.utc)


def make_entry(fixture_id, kickoff_utc, probability=0.65, home="Дом", away="Гости"):
    fixture = Fixture(
        fixture_id=fixture_id, kickoff_utc=kickoff_utc, home_team=home, away_team=away,
        home_team_id=1, away_team_id=2, league_name="Test League",
        league_country="World", status_short="NS",
    )
    candidate = MarketCandidate(
        fixture=fixture, market_key="h2h_home", market_label_ru="Победа хозяев",
        probability=probability, completeness=1.0, sample_size_category="full",
        rationale="test", source="recent_form",
    )
    rec = RankedRecommendation(candidate=candidate, signal_level="HIGH")
    return rec, 1.9, "TestBookmaker"


# -- (a) lead-time buffer: exactly-started, imminent, and safely-future ----
started = make_entry(1, NOW - datetime.timedelta(minutes=5))
imminent = make_entry(2, NOW + datetime.timedelta(minutes=10))  # inside 30-min buffer
right_at_buffer = make_entry(3, NOW + datetime.timedelta(minutes=MIN_LEAD_TIME_MINUTES))  # exactly the edge, must be excluded
safely_future = make_entry(4, NOW + datetime.timedelta(hours=2))

check("has_enough_lead_time excludes an already-started fixture", not has_enough_lead_time(started[0].candidate.fixture.kickoff_utc, NOW))
check("has_enough_lead_time excludes a fixture inside the 30-min buffer", not has_enough_lead_time(imminent[0].candidate.fixture.kickoff_utc, NOW))
check("has_enough_lead_time excludes a fixture exactly at the buffer edge", not has_enough_lead_time(right_at_buffer[0].candidate.fixture.kickoff_utc, NOW))
check("has_enough_lead_time keeps a safely-future fixture", has_enough_lead_time(safely_future[0].candidate.fixture.kickoff_utc, NOW))

ranked_pool = [e[0] for e in [started, imminent, right_at_buffer, safely_future]]
selected = select_current_recommendations(ranked_pool, NOW)
selected_ids = {r.candidate.fixture.fixture_id for r in selected}
check("select_current_recommendations keeps only the safely-future fixture", selected_ids == {4}, selected_ids)

# -- (b) later request automatically picks fresh candidates once morning
# picks have started, from the SAME persisted pool, no new quota spent --
# 5 top-ranked picks all kick off soon (still >=30min out at NOW, but
# will have started an hour later), plus one lower-ranked backup that
# only fits once a slot opens up -- the pool is already best-first
# ordered, exactly the contract rank_all_candidates guarantees.
top_picks = [make_entry(10 + i, NOW + datetime.timedelta(minutes=45), probability=0.9 - i * 0.01) for i in range(MAX_RECOMMENDATIONS)]
backup_pick = make_entry(20, NOW + datetime.timedelta(hours=5), probability=0.55)
pool = top_picks + [backup_pick]

morning_selection = select_current_recommendations([e[0] for e in pool], NOW)
check("morning request fills up to the cap with the higher-ranked, still-startable picks",
      [r.candidate.fixture.fixture_id for r in morning_selection] == [10 + i for i in range(MAX_RECOMMENDATIONS)])

evening_now = NOW + datetime.timedelta(hours=1)  # the 5 top picks (45 min out) have now started
evening_selection = select_current_recommendations([e[0] for e in pool], evening_now)
check("evening request automatically substitutes the backup once the top picks have started",
      [r.candidate.fixture.fixture_id for r in evening_selection] == [20])

# -- never pads below max_count with a weaker/fake candidate --------------
lone_pool = [safely_future[0]]
lone_selection = select_current_recommendations(lone_pool, NOW, max_count=MAX_RECOMMENDATIONS)
check("selection is never padded past however many real candidates remain",
      len(lone_selection) == 1)

# -- (c)/(d) end-to-end via the real archive + tracking/analytics dedup ---
db_path = tempfile.mktemp()
cache = FootballCache(db_path=db_path, now=NOW)
result = FootballPipelineResult(pool=pool, found_fixtures=len(pool), analysed_fixtures=len(pool), matched_fixtures=len(pool))
save_daily_archive(cache, result, NOW)
archive = load_daily_archive(cache, NOW)
check("archive round-trips the full pool, not a sliced top-N", len(archive.pool) == len(pool), len(archive.pool))

tracking_db = tempfile.mktemp()
analytics_db = tempfile.mktemp()
storage = TrackingStorage(db_path=tracking_db)
analytics_storage = AnalyticsStorage(db_path=analytics_db, now=NOW)

messages_morning, entries_morning, saved_morning, dup_morning = reselect_from_archive(
    archive.pool, archive.diagnostics, NOW, storage=storage, analytics_storage=analytics_storage,
)
check("morning reselect_from_archive persists all newly-surfaced picks", saved_morning == MAX_RECOMMENDATIONS and dup_morning == 0, (saved_morning, dup_morning))
check("morning reselect_from_archive renders the morning picks", any("Победа хозяев" in m for m in messages_morning))

# Same request again a moment later (still the same picks valid): must be
# a safe no-op duplicate, never a crash or a double-save.
_, _, saved_again, dup_again = reselect_from_archive(
    archive.pool, archive.diagnostics, NOW, storage=storage, analytics_storage=analytics_storage,
)
check("repeating the same still-valid selection is a safe duplicate, not a re-save", saved_again == 0 and dup_again == MAX_RECOMMENDATIONS, (saved_again, dup_again))

# Evening: the top picks have started, so the backup surfaces and gets
# saved for the FIRST time -- proving re-selection persists newly-eligible
# picks without erroring on the already-saved (but no longer selected) ones.
messages_evening, entries_evening, saved_evening, dup_evening = reselect_from_archive(
    archive.pool, archive.diagnostics, evening_now, storage=storage, analytics_storage=analytics_storage,
)
check("evening reselect_from_archive picks the backup fixture", [e[0].candidate.fixture.fixture_id for e in entries_evening] == [20])
check("evening reselect_from_archive persists the newly-surfaced backup pick", saved_evening == 1 and dup_evening == 0, (saved_evening, dup_evening))

storage.close()
analytics_storage.close()
cache.close()

failed = [r for r in results if r[1] == "FAIL"]
print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
if failed:
    raise SystemExit(1)
