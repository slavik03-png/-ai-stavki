"""
Minimum filters (spec section 12/13).

Every candidate must pass every rule here before it is even considered by
diversification/ranking. Filters never invent data to pass a candidate --
missing data is a rejection reason, not a neutral default.

`apply_filters` returns (passed: bool, reasons: List[str]) and never
mutates the candidate's group -- that is the selector's job.
"""

from __future__ import annotations

import datetime
from typing import List, Sequence, Set, Tuple

from selection_engine.config import MARKET_DATA_REQUIREMENTS, SelectionConfig, market_requirements_for
from selection_engine.models import CandidatePrediction
from selection_engine.scoring import missing_required_fields


def _parse_iso(ts: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def apply_filters(
    candidate: CandidatePrediction,
    config: SelectionConfig,
    *,
    now: datetime.datetime,
    seen_dedup_keys: Set[str],
    is_market_suspended: bool = False,
) -> Tuple[bool, List[str]]:
    reasons: List[str] = []

    # 1. Valid odds
    if candidate.odds is None or candidate.odds <= 1.0:
        reasons.append("Некорректный коэффициент (должен быть больше 1.0)")
    elif candidate.odds < config.min_decimal_odds:
        reasons.append(
            f"Коэффициент {candidate.odds:.2f} ниже минимального порога {config.min_decimal_odds:.2f}"
        )

    # 2. Market allowed / not disabled
    allowed = config.effective_allowed_markets()
    if candidate.market_type not in allowed:
        reasons.append(f"Рынок '{candidate.market_type}' отключён в конфигурации")

    # 3. Market must be one this engine (via tracking) can settle
    if candidate.market_type not in MARKET_DATA_REQUIREMENTS:
        reasons.append(f"Неизвестный/неподдерживаемый тип рынка '{candidate.market_type}'")

    # 4. Required data present
    market_reqs = market_requirements_for(candidate.market_type, config)
    missing = missing_required_fields(candidate.available_fields, market_reqs["required"])
    if missing:
        reasons.append("Отсутствуют обязательные данные: " + ", ".join(missing))

    # 5. Negative expected value
    if candidate.expected_value is not None and candidate.expected_value < config.min_expected_value:
        reasons.append(
            f"Ожидаемая ценность {candidate.expected_value:+.3f} ниже минимума {config.min_expected_value:+.3f}"
        )

    # 6. Edge too small
    if candidate.edge is not None and candidate.edge < config.min_edge:
        reasons.append(f"Edge {candidate.edge:+.3f} ниже минимума {config.min_edge:+.3f}")

    # 7. Confidence below minimum
    if candidate.confidence_score is not None and candidate.confidence_score < config.min_confidence_score:
        reasons.append(
            f"Уверенность {candidate.confidence_score:.1f} ниже минимального порога {config.min_confidence_score:.1f}"
        )

    # 8. Data completeness below minimum
    if candidate.data_completeness is not None and candidate.data_completeness < config.min_data_completeness:
        reasons.append(
            f"Полнота данных {candidate.data_completeness:.0%} ниже минимума {config.min_data_completeness:.0%}"
        )

    # 9. Event already started
    try:
        match_dt = _parse_iso(candidate.match_datetime)
        if match_dt <= now:
            reasons.append("Событие уже началось или завершилось")
    except (ValueError, TypeError):
        reasons.append("Некорректная дата/время события")

    # 10. Stale price
    if candidate.price_timestamp:
        try:
            price_dt = _parse_iso(candidate.price_timestamp)
            age_minutes = (now - price_dt).total_seconds() / 60.0
            if age_minutes > config.max_price_age_minutes:
                reasons.append(
                    f"Котировка устарела ({age_minutes:.0f} мин. назад, лимит {config.max_price_age_minutes:.0f} мин.)"
                )
        except (ValueError, TypeError):
            reasons.append("Некорректная метка времени котировки")

    # 11. Duplicate within this batch
    try:
        key = candidate.dedup_key
        if key in seen_dedup_keys:
            reasons.append("Дублирующая рекомендация (тот же исход уже выбран)")
    except Exception:
        reasons.append("Не удалось вычислить ключ дедупликации")

    # 12. Suspended market
    if is_market_suspended:
        reasons.append("Рынок временно приостановлен букмекером")

    # 13. Odds too high for MAIN-tier consideration is handled later per group
    # (max_decimal_odds_main), not a hard rejection here, since such a
    # candidate may still be legitimate as HIGH_RISK/RESERVE.

    candidate.rejection_reasons = list(reasons)
    return (len(reasons) == 0, reasons)


def filter_candidates(
    candidates: Sequence[CandidatePrediction],
    config: SelectionConfig,
    *,
    now: datetime.datetime,
    suspended_dedup_keys: Set[str] = frozenset(),
) -> Tuple[List[CandidatePrediction], List[CandidatePrediction]]:
    """Returns (passed, rejected). Order of `candidates` is preserved within
    each list. Duplicate detection is stateful across the whole batch: the
    first occurrence of a dedup key passes (if otherwise valid), subsequent
    occurrences are rejected as duplicates."""
    passed: List[CandidatePrediction] = []
    rejected: List[CandidatePrediction] = []
    seen: Set[str] = set()

    for candidate in candidates:
        is_suspended = False
        try:
            is_suspended = candidate.dedup_key in suspended_dedup_keys
        except Exception:
            pass
        ok, _reasons = apply_filters(
            candidate, config, now=now, seen_dedup_keys=seen, is_market_suspended=is_suspended
        )
        if ok:
            try:
                seen.add(candidate.dedup_key)
            except Exception:
                pass
            passed.append(candidate)
        else:
            rejected.append(candidate)

    return passed, rejected
