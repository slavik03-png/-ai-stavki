"""
Recommendation engine -- turns a list of football.prediction.MarketResult
into a ranked, de-duplicated recommendation report.

Rules:
- Unavailable markets are dropped entirely (nothing to recommend).
- Markets are grouped by `family` (assigned in prediction.py) so that
  near-identical markets (e.g. Over 0.5 / 1.5 / 2.5 / 3.5 goals) don't all
  compete for the top slots -- only the strongest representative of each
  family is eligible for "main" or "alternatives".
- Correct-score markets are never eligible for "main" or "alternatives":
  they always land in `high_risk`, regardless of confidence.
- If no market clears the "medium confidence" bar, `main` is None and a
  clear "no reliable recommendation" message is produced.
- No recommendation is ever described as guaranteed, certain, or safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from football.prediction import MarketResult, STATUS_UNAVAILABLE

# Confidence-band interpretation (used for narrative labels only).
BAND_VERY_HIGH = (85, 100, "очень высокая уверенность")
BAND_HIGH = (75, 85, "высокая уверенность")
BAND_MEDIUM = (65, 75, "средняя уверенность")
BAND_LOW = (55, 65, "низкая уверенность")
BAND_NOT_RECOMMENDED = (0, 55, "недостаточно уверенности")

MAIN_MIN_CONFIDENCE = 65.0
ALTERNATIVE_MIN_CONFIDENCE = 55.0
AVOID_MAX_CONFIDENCE = 45.0

NO_RELIABLE_RECOMMENDATION = "Надёжная рекомендация отсутствует"


def confidence_band_label(confidence: float) -> str:
    for lo, hi, label in (BAND_VERY_HIGH, BAND_HIGH, BAND_MEDIUM, BAND_LOW, BAND_NOT_RECOMMENDED):
        if lo <= confidence <= hi if hi == 100 else lo <= confidence < hi:
            return label
    return BAND_NOT_RECOMMENDED[2]


@dataclass
class RecommendationReport:
    main: Optional[MarketResult]
    alternatives: List[MarketResult] = field(default_factory=list)
    high_risk: List[MarketResult] = field(default_factory=list)
    avoid: List[MarketResult] = field(default_factory=list)
    no_reliable_recommendation: bool = False
    message: Optional[str] = None


def _best_per_family(markets: List[MarketResult]) -> List[MarketResult]:
    """Keeps only the highest-confidence market for each `family`, preserving
    the rest for reference but excluding them from the deduplicated list."""
    best: dict = {}
    for m in markets:
        key = m.family or m.market_name
        current = best.get(key)
        if current is None or m.confidence > current.confidence:
            best[key] = m
    return sorted(best.values(), key=lambda m: m.confidence, reverse=True)


def build_recommendation(market_results: List[MarketResult]) -> RecommendationReport:
    available = [m for m in market_results if m.status != STATUS_UNAVAILABLE]

    correct_score = [m for m in available if m.market_type == "correct_score"]
    non_correct_score = [m for m in available if m.market_type != "correct_score"]

    deduped = _best_per_family(non_correct_score)

    main_candidates = [m for m in deduped if m.confidence >= MAIN_MIN_CONFIDENCE]
    alt_candidates = [
        m for m in deduped
        if ALTERNATIVE_MIN_CONFIDENCE <= m.confidence < MAIN_MIN_CONFIDENCE
    ]
    avoid_candidates = [m for m in deduped if m.confidence < AVOID_MAX_CONFIDENCE]
    # Anything not main/alternative/avoid and not correct-score, but flagged
    # risky by the engine itself, surfaces as a high-risk option.
    high_risk_candidates = [
        m for m in deduped
        if m not in main_candidates and m not in avoid_candidates
        and (m.risk == "высокий" or (AVOID_MAX_CONFIDENCE <= m.confidence < ALTERNATIVE_MIN_CONFIDENCE))
    ]

    if not main_candidates:
        return RecommendationReport(
            main=None,
            alternatives=alt_candidates[:3],
            high_risk=high_risk_candidates[:3] + sorted(correct_score, key=lambda m: m.confidence, reverse=True),
            avoid=avoid_candidates[:5],
            no_reliable_recommendation=True,
            message=NO_RELIABLE_RECOMMENDATION,
        )

    main = main_candidates[0]
    remaining_main_pool = [m for m in main_candidates[1:] if m.family != main.family]
    alternatives = (remaining_main_pool + [
        m for m in alt_candidates if m.family != main.family
    ])[:3]

    used_families = {main.family} | {m.family for m in alternatives}
    high_risk = [m for m in high_risk_candidates if m.family not in used_families][:3]
    high_risk = high_risk + sorted(correct_score, key=lambda m: m.confidence, reverse=True)

    avoid = [m for m in avoid_candidates if m.family not in used_families][:5]

    return RecommendationReport(
        main=main,
        alternatives=alternatives,
        high_risk=high_risk,
        avoid=avoid,
        no_reliable_recommendation=False,
        message=None,
    )
