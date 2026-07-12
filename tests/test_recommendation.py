"""
Tests for the recommendation ranking layer (football/recommendation.py).
"""

import sys

sys.path.insert(0, ".")

from football.providers.mock_provider import MockFootballProvider
from football.prediction import analyze_match
from football.recommendation import build_recommendation

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


provider = MockFootballProvider()


def run():
    ctx, markets = analyze_match(provider, "Mock Home FC", "Mock Away FC", "Mock League")
    rec = build_recommendation(markets)

    check("recommendation object has main/alternatives/high_risk/avoid fields",
          all(hasattr(rec, f) for f in ("main", "alternatives", "high_risk", "avoid", "no_reliable_recommendation")))

    if rec.main is not None:
        check("main recommendation confidence is 65 or higher", rec.main.confidence >= 65,
              rec.main.confidence)

    correct_score_names = {"family" for m in markets if m.market_type == "correct_score"}
    high_risk_correct_scores = [m for m in rec.high_risk if m.market_type == "correct_score"]
    all_correct_scores = [m for m in markets if m.market_type == "correct_score"]
    check("every generated correct-score market ends up in high_risk",
          len(high_risk_correct_scores) == len(all_correct_scores),
          (len(high_risk_correct_scores), len(all_correct_scores)))

    check("correct-score never appears in main", rec.main is None or rec.main.market_type != "correct_score")
    check("correct-score never appears in alternatives",
          all(m.market_type != "correct_score" for m in rec.alternatives))

    families_in_main_and_alts = [rec.main.family] if rec.main else []
    families_in_main_and_alts += [m.family for m in rec.alternatives]
    check("no duplicate market family across main+alternatives",
          len(families_in_main_and_alts) == len(set(families_in_main_and_alts)),
          families_in_main_and_alts)

    ctx_weak, markets_weak = analyze_match(provider, "Unknown A", "Unknown B", "Unknown League")
    rec_weak = build_recommendation(markets_weak)
    check("weak evidence produces no_reliable_recommendation with no main pick",
          rec_weak.no_reliable_recommendation and rec_weak.main is None and bool(rec_weak.message))

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
