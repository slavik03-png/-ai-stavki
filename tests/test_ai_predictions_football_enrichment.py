"""
Tests for the API-Football enrichment activation: quota-protecting cache,
fuzzy team matching, and the enrichment step's honest, tier-preserving
blending. Run via tests/test_ai_predictions_regression.py.
"""
import datetime
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_predictions.football_cache import FootballCache
from ai_predictions.football_matching import best_team_match
from ai_predictions.value_engine import ValueCandidate, compute_combined_score
from ai_predictions.value_selector import _sort_key
from ai_predictions.enrichment import enrich_candidates
from ai_predictions.value_config import API_FOOTBALL_DAILY_QUOTA, API_FOOTBALL_QUOTA_RESERVE

results = []


def check(name, condition, detail=None):
    status = "PASS" if condition else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail if detail is not None else ''}")
    return condition


def _tmp_cache():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return FootballCache(db_path=path, now=datetime.datetime(2026, 7, 13, tzinfo=datetime.timezone.utc))


def test_cache_hit_within_ttl_and_miss_after():
    cache = _tmp_cache()
    cache.set("k1", {"v": 1})
    check("cached value is returned within TTL", cache.get("k1") == {"v": 1})

    later = FootballCache(db_path=cache.db_path, now=datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc))
    check("cached value expires after 24h TTL", later.get("k1") is None)
    cache.close()
    later.close()


def test_quota_never_exceeds_reserve_boundary():
    cache = _tmp_cache()
    budget = API_FOOTBALL_DAILY_QUOTA - API_FOOTBALL_QUOTA_RESERVE
    check("full budget available at start of day", cache.requests_available() == budget)
    cache.record_requests(budget)
    check("cannot spend once budget is exhausted", cache.can_spend(1) is False)
    check("requests_available never goes negative", cache.requests_available() == 0)
    cache.close()


def test_quota_persists_across_new_connections_same_day():
    cache = _tmp_cache()
    cache.record_requests(5)
    reopened = FootballCache(db_path=cache.db_path, now=datetime.datetime(2026, 7, 13, 20, tzinfo=datetime.timezone.utc))
    check("usage persists across reconnects on the same day", reopened.requests_used_today() == 5)
    cache.close()
    reopened.close()


def test_fuzzy_match_accepts_close_name_rejects_unrelated():
    candidates = [
        {"id": 1, "name": "Manchester United FC", "country": "England"},
        {"id": 2, "name": "Manchester City FC", "country": "England"},
    ]
    match = best_team_match("Man United", candidates)
    check("close real name is accepted with high confidence", match.matched and match.team_id == 1, match)

    no_match = best_team_match("Some Totally Different Club", candidates)
    check("unrelated name is honestly rejected, not guessed", not no_match.matched, no_match)


def test_fuzzy_match_handles_empty_candidates():
    result = best_team_match("Any Team", [])
    check("empty candidate list is an honest non-match, not a crash", not result.matched)


def _candidate(event_id, home, away, signal_level="MEDIUM", ranking_score=1.0):
    return ValueCandidate(
        event_id=event_id, sport="soccer", league="Test League", country=None,
        match_datetime="2026-07-14T18:00:00Z", home_team=home, away_team=away,
        market_type="1x2", selection=home, line=None,
        best_bookmaker="X", best_price=2.0, best_price_implied_probability=0.5,
        consensus_probability=0.52, consensus_bookmaker_count=4, fair_price=1.9,
        edge=0.05, expected_value=0.05, bookmaker_count=5, unique_bookmaker_count=5,
        signal_level=signal_level, ranking_score=ranking_score,
    )


def test_enrichment_without_api_key_is_zero_cost_and_honest():
    candidates = [_candidate("e1", "Home FC", "Away FC")]
    summary = enrich_candidates(candidates, api_key=None)
    check("no API key -> zero requests spent", summary.api_football_requests_used == 0)
    check("candidate marked unavailable, not silently left as 'not_attempted'", candidates[0].statistics_source == "unavailable")
    check("ranking_score is unchanged when statistics never ran", candidates[0].final_combined_score == candidates[0].ranking_score)


def test_enrichment_out_of_range_season_skips_all_calls():
    candidates = [_candidate("e1", "Home FC", "Away FC")]
    now = datetime.datetime(2026, 7, 13, tzinfo=datetime.timezone.utc)
    summary = enrich_candidates(candidates, api_key="fake-key", now=now, cache=_tmp_cache())
    check("2026 season is outside the free-plan allowed range -> season_allowed is False", summary.season_allowed is False)
    check("out-of-range season spends exactly zero requests", summary.api_football_requests_used == 0)
    check("skipped reason is present and honest", summary.skipped_reason is not None and "сезон" in summary.skipped_reason.lower() or "Сезон" in (summary.skipped_reason or ""))
    check("candidate's odds-only ranking is preserved", candidates[0].final_combined_score == candidates[0].ranking_score)


def test_combined_score_never_reorders_across_tiers():
    high = _candidate("e1", "Home FC", "Away FC", signal_level="HIGH", ranking_score=1.0)
    medium = _candidate("e2", "Home FC", "Away FC", signal_level="MEDIUM", ranking_score=5.0)
    # Even if statistics strongly favor `medium` and disfavor `high`,
    # the HIGH tier must still sort first.
    high.statistics_score = 0.1
    high.statistics_completeness = 1.0
    high.final_combined_score = compute_combined_score(high)
    medium.statistics_score = 0.95
    medium.statistics_completeness = 1.0
    medium.final_combined_score = compute_combined_score(medium)
    ordered = sorted([medium, high], key=_sort_key)
    check("HIGH always sorts before MEDIUM regardless of statistics nudge", ordered[0] is high, [c.signal_level for c in ordered])


def test_combined_score_neutral_statistics_does_not_change_score():
    candidate = _candidate("e1", "Home FC", "Away FC", ranking_score=3.0)
    candidate.statistics_score = 0.5
    candidate.statistics_completeness = 1.0
    check("neutral (0.5) statistics score leaves ranking_score unchanged", compute_combined_score(candidate) == candidate.ranking_score)


def run():
    test_cache_hit_within_ttl_and_miss_after()
    test_quota_never_exceeds_reserve_boundary()
    test_quota_persists_across_new_connections_same_day()
    test_fuzzy_match_accepts_close_name_rejects_unrelated()
    test_fuzzy_match_handles_empty_candidates()
    test_enrichment_without_api_key_is_zero_cost_and_honest()
    test_enrichment_out_of_range_season_skips_all_calls()
    test_combined_score_never_reorders_across_tiers()
    test_combined_score_neutral_statistics_does_not_change_score()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
