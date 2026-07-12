"""
Tests for the Russian-language report generator (football/report_ai.py).
"""

import re
import sys

sys.path.insert(0, ".")

from football.providers.mock_provider import MockFootballProvider
from football.prediction import analyze_match
from football.recommendation import build_recommendation
from football.report_ai import render_report_ru, FINAL_DISCLAIMER

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


provider = MockFootballProvider()


def run():
    ctx, markets = analyze_match(provider, "Mock Home FC", "Mock Away FC", "Mock League")
    rec = build_recommendation(markets)
    report = render_report_ru(ctx, markets, rec)

    required_sections = [
        "1. Матч", "2. Качество и полнота данных", "3. Форма команд", "4. Домашняя и выездная форма",
        "5. Личные встречи", "6. Голы", "7. Обе забьют", "8. Первый тайм", "9. Второй тайм",
        "10. Угловые", "11. Карточки", "12. Фолы", "13. Удары и удары в створ", "14. Турнирное положение",
        "15. Составы, травмы и отсутствующие игроки", "16. Основная рекомендация", "17. Альтернативные варианты",
        "18. Рискованные варианты", "19. Рынки, которые лучше пропустить", "20. Итоговое предупреждение",
    ]
    missing_sections = [s for s in required_sections if s not in report]
    check("Russian report contains all 20 required sections", not missing_sections, missing_sections)

    check("final disclaimer present", FINAL_DISCLAIMER in report)

    report_stripped = re.sub(r"не\s+гарантир\w*", "", report, flags=re.IGNORECASE)
    forbidden_words = ["гарантир", "100%", "точно выиграет", "верняк", "safe bet", "guaranteed", "certain"]
    found_forbidden = [w for w in forbidden_words if w.lower() in report_stripped.lower()]
    check("no affirmative forbidden wording (guaranteed/certain/100%) in report", not found_forbidden, found_forbidden)

    check("report is non-trivial in length", len(report) > 2000, len(report))

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
