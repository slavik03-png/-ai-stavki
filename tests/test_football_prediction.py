"""
Tests for the deterministic confidence/prediction engine
(football/prediction.py) against MockFootballProvider.
"""

import sys

sys.path.insert(0, ".")

from football.providers.mock_provider import MockFootballProvider
from football.prediction import analyze_match, STATUS_UNAVAILABLE, Fraction, compute_confidence

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


provider = MockFootballProvider()
HOME, AWAY, LEAGUE = "Mock Home FC", "Mock Away FC", "Mock League"


def run():
    ctx1, markets1 = analyze_match(provider, HOME, AWAY, LEAGUE)

    required_fields = ["market_name", "market_type", "confidence", "strength", "risk", "stars",
                        "explanation", "supporting_statistics", "missing_statistics", "status"]
    check("all markets expose required structure",
          all(hasattr(m, f) for m in markets1 for f in required_fields))

    expected_market_names = {
        "Победа хозяев", "Ничья", "Победа гостей", "Двойной шанс 1X", "Двойной шанс X2", "Двойной шанс 12",
        "Тотал больше 0.5", "Тотал больше 1.5", "Тотал больше 2.5", "Тотал больше 3.5", "Тотал меньше 2.5",
        "Обе забьют — Да", "Обе забьют — Нет", "Гол в обоих таймах",
        f"{HOME}: тотал больше 0.5", f"{HOME}: тотал больше 1.5", f"{AWAY}: тотал больше 0.5", f"{AWAY}: тотал больше 1.5",
        "Первый тайм больше 0.5", "Первый тайм больше 1.0", "Второй тайм больше 0.5", "Второй тайм больше 1.0",
        "Обе забьют в первом тайме",
        f"{HOME} забьёт в первом тайме", f"{AWAY} забьёт в первом тайме",
        f"{HOME} забьёт во втором тайме", f"{AWAY} забьёт во втором тайме",
    }
    present_names = {m.market_name for m in markets1}
    check("all match-result/goals/half markets present", expected_market_names.issubset(present_names),
          expected_market_names - present_names)

    additional_families = {m.family for m in markets1 if m.market_type == "additional_stats"}
    check("corners/cards/fouls/shots/shots_on_target markets present",
          {"corners_over", "cards_over", "fouls_over", "shots_over", "shots_on_target_over"}.issubset(additional_families),
          additional_families)

    correct_score_markets = [m for m in markets1 if m.market_type == "correct_score"]
    check("up to 3 correct-score candidates generated", 0 < len(correct_score_markets) <= 3, len(correct_score_markets))

    check("confidence always in [0,100]", all(0 <= m.confidence <= 100 for m in markets1))
    check("stars always in [1,5]", all(1 <= m.stars <= 5 for m in markets1))

    ctx_missing, markets_missing = analyze_match(provider, HOME, "Unknown Rival FC", LEAGUE)
    home_win_full = next(m for m in markets1 if m.market_name == "Победа хозяев")
    home_win_gap = next(m for m in markets_missing if m.market_name == "Победа хозяев")
    check("missing data lowers or nullifies confidence vs full data",
          home_win_gap.status == STATUS_UNAVAILABLE or home_win_gap.confidence <= home_win_full.confidence,
          (home_win_full.confidence, home_win_gap.confidence, home_win_gap.status))

    unavailable_markets = [m for m in markets_missing if m.status == STATUS_UNAVAILABLE]
    check("unavailable markets have missing_statistics recorded, not fabricated support",
          all(len(m.missing_statistics) > 0 and len(m.supporting_statistics) == 0 for m in unavailable_markets))

    ctx2, markets2 = analyze_match(provider, HOME, AWAY, LEAGUE)
    same_output = all(
        (a.market_name, a.confidence, a.stars, a.status, a.explanation) ==
        (b.market_name, b.confidence, b.stars, b.status, b.explanation)
        for a, b in zip(markets1, markets2)
    )
    check("same input produces same output (deterministic)", same_output)

    broad_available = [m.confidence for m in markets1 if m.market_type != "correct_score" and m.status != STATUS_UNAVAILABLE]
    if broad_available and correct_score_markets:
        max_broad = max(broad_available)
        check("correct-score confidence lower than broader markets",
              all(cs.confidence < max_broad for cs in correct_score_markets),
              (max_broad, [cs.confidence for cs in correct_score_markets]))

    conf_agree, contra_agree = compute_confidence([Fraction(8, 10), Fraction(8, 10)], 0)
    conf_disagree, contra_disagree = compute_confidence([Fraction(9, 10), Fraction(1, 10)], 0)
    check("contradictory fractions produce lower confidence than agreeing ones",
          conf_disagree < conf_agree and contra_disagree is True and contra_agree is False,
          (conf_agree, conf_disagree, contra_agree, contra_disagree))

    bad_explanations = [m for m in markets1 if m.status == STATUS_UNAVAILABLE and m.explanation]
    check("unavailable markets carry no explanation text", not bad_explanations)

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
