"""
Tests for tracking/report.py (Russian report) and tracking/telegram_adapter.py.
"""

import re
import sys

sys.path.insert(0, ".")

from tracking.report import render_report_ru, FINAL_DISCLAIMER
from tracking import telegram_adapter as tg

results = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((name, status))
    print(f"[{status}] {name} {detail}")


class FakeRow(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def _row(status, **overrides):
    base = dict(
        status=status, bookmaker_odds=2.0, created_at="2026-07-01T00:00:00+00:00",
        sport="football", league="Premier League", market_type="1x2",
        recommendation_group="main", confidence_level="средняя уверенность",
        model_version="v1", home_team="Arsenal", away_team="Chelsea",
        market_name="Победа хозяев", confidence_score=68.0, settled_at=None,
        final_score=None,
    )
    base.update(overrides)
    return FakeRow(base)


def test_report_contains_required_sections():
    rows = [_row("won"), _row("lost"), _row("pending")]
    report = render_report_ru(rows)
    required_titles = [
        "Общие результаты", "Открытые прогнозы", "Сильнейшие рынки", "Слабейшие рынки",
        "уровню уверенности", "группе рекомендаций", "Последние 7 дней", "Последние 30 дней",
        "Итоговое предупреждение",
    ]
    missing = [t for t in required_titles if t not in report]
    check("all required report sections are present", not missing, missing)


def test_report_has_disclaimer_and_no_forbidden_claims():
    rows = [_row("won") for _ in range(30)]
    report = render_report_ru(rows)
    check("final disclaimer present", FINAL_DISCLAIMER in report)
    lowered = report.lower()
    # "гарантир" is only allowed as part of a negated disclaimer ("не гарантирует").
    affirmative_guarantee = re.search(r"(?<!не )(?<!не  )гарантир", lowered)
    forbidden_phrases = ["100% побед", "точно выиграет"]
    hits = [w for w in forbidden_phrases if w in lowered]
    check("no guaranteed-profit language", not affirmative_guarantee and not hits,
          (affirmative_guarantee, hits))


def test_report_warns_on_small_sample():
    rows = [_row("won"), _row("lost")]
    report = render_report_ru(rows)
    check("small-sample warning present when decisive count is low", "мала" in report)


def test_telegram_adapter_outputs():
    rows = [_row("won"), _row("pending"), _row("lost", market_type="total_goals")]
    stats_text = tg.statistics_summary_text(rows)
    open_text = tg.open_predictions_text(rows)
    recent_text = tg.recent_results_text([_row("won", settled_at="2026-07-05T00:00:00+00:00", final_score="2:1")])
    market_text = tg.by_market_text(rows)
    confidence_text = tg.by_confidence_text(rows)

    check("statistics_summary_text uses the button label", tg.BTN_STATISTICS in stats_text)
    check("open_predictions_text uses the button label", tg.BTN_OPEN in open_text)
    check("recent_results_text uses the button label", tg.BTN_RECENT in recent_text)
    check("by_market_text uses the button label", tg.BTN_BY_MARKET in market_text)
    check("by_confidence_text uses the button label", tg.BTN_BY_CONFIDENCE in confidence_text)
    check("recent_results_text shows the final score", "2:1" in recent_text)


def run():
    test_report_contains_required_sections()
    test_report_has_disclaimer_and_no_forbidden_claims()
    test_report_warns_on_small_sample()
    test_telegram_adapter_outputs()

    failed = [r for r in results if r[1] == "FAIL"]
    print(f"\n==SUMMARY== {len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
