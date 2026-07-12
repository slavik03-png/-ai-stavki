"""
End-to-end selection pipeline: scoring -> calibration -> filters ->
diversification -> ranking -> group assignment -> truncation.

`select_recommendations` is the single public entry point. It never pads
output to reach a quota: if fewer than max_main qualify, fewer are
returned; if zero qualify, an explicit empty result (with reasons) is
returned instead of forcing weak picks through.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from selection_engine.calibration import build_calibration_buckets, calibrate_probability
from selection_engine.config import SelectionConfig
from selection_engine.diversification import diversify
from selection_engine.filters import filter_candidates
from selection_engine.models import (
    GROUP_AVOID,
    GROUP_HIGH_RISK,
    GROUP_INSUFFICIENT_DATA,
    GROUP_MAIN,
    GROUP_RESERVE,
    CandidatePrediction,
)
from selection_engine.scoring import compute_selection_score, score_candidate


@dataclass
class SelectionResult:
    generated_at: str
    main: List[CandidatePrediction] = field(default_factory=list)
    reserve: List[CandidatePrediction] = field(default_factory=list)
    high_risk: List[CandidatePrediction] = field(default_factory=list)
    avoid: List[CandidatePrediction] = field(default_factory=list)
    insufficient_data: List[CandidatePrediction] = field(default_factory=list)
    rejected: List[CandidatePrediction] = field(default_factory=list)
    total_candidates_considered: int = 0
    no_recommendation_reasons: List[str] = field(default_factory=list)

    @property
    def has_main_recommendations(self) -> bool:
        return len(self.main) > 0


def _historical_lookup(storage, market_type: str, model_version: str) -> Dict[str, Optional[float]]:
    """Pulls a market-specific and model-version-specific historical win
    rate/ROI from tracking.statistics, if a storage handle was supplied and
    enough decisive history exists. Returns None values when there is no
    storage or no reliable sample -- callers must treat None as "unknown",
    never as zero."""
    if storage is None:
        return {
            "market_win_rate": None, "market_roi": None,
            "model_win_rate": None, "model_roi": None,
        }
    from tracking.statistics import by_market_type, by_model_version

    all_predictions = storage.list_all_predictions()
    market_buckets = by_market_type(all_predictions)
    version_buckets = by_model_version(all_predictions)

    market_win_rate = None
    market_roi = None
    market_bucket = market_buckets.get(market_type)
    if market_bucket is not None and market_bucket.win_rate is not None:
        # Stats.win_rate/roi are on a 0..100 percent scale; CandidatePrediction
        # historical_market_win_rate is documented as a 0..1 probability, so
        # only win_rate is rescaled here -- roi stays a percent (matches
        # historical_market_roi's own percent documentation).
        market_win_rate = market_bucket.win_rate / 100.0
        market_roi = market_bucket.roi

    model_win_rate = None
    model_roi = None
    version_bucket = version_buckets.get(model_version)
    if version_bucket is not None and version_bucket.win_rate is not None:
        model_win_rate = version_bucket.win_rate / 100.0
        model_roi = version_bucket.roi

    return {
        "market_win_rate": market_win_rate, "market_roi": market_roi,
        "model_win_rate": model_win_rate, "model_roi": model_roi,
    }


def _assign_group(
    candidate: CandidatePrediction,
    config: SelectionConfig,
    *,
    is_diversified_out: bool,
) -> str:
    if candidate.market_type in config.high_risk_markets and not config.allow_high_risk_as_main:
        return GROUP_HIGH_RISK
    if candidate.data_completeness is not None and candidate.data_completeness < config.min_data_completeness:
        return GROUP_INSUFFICIENT_DATA
    if is_diversified_out:
        return GROUP_RESERVE
    if candidate.confidence_score is not None and candidate.confidence_score >= config.preferred_confidence_score \
            and candidate.odds <= config.max_decimal_odds_main:
        return GROUP_MAIN
    return GROUP_RESERVE


def select_recommendations(
    raw_candidates: Sequence[CandidatePrediction],
    config: Optional[SelectionConfig] = None,
    *,
    storage=None,
    now: Optional[datetime.datetime] = None,
) -> SelectionResult:
    """Runs the full pipeline over `raw_candidates` (each already carrying
    its raw model_probability/odds/available_fields/sample_size -- this
    function does not fetch or invent statistics itself).

    `storage`, if given, must be a `tracking.storage.TrackingStorage`
    instance; it is used read-only to pull historical win rates/ROI and to
    build calibration buckets from previously settled predictions. Passing
    None skips calibration/historical adjustment entirely (every candidate
    is then scored on its own raw inputs, marked preliminary)."""
    if config is None:
        config = SelectionConfig()
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)

    total_considered = len(raw_candidates)

    calibration_buckets = {}
    if storage is not None:
        try:
            all_predictions = storage.list_all_predictions()
            calibration_buckets = build_calibration_buckets(all_predictions, config)
        except Exception:
            calibration_buckets = {}

    scored: List[CandidatePrediction] = []
    for candidate in raw_candidates:
        historical = _historical_lookup(storage, candidate.market_type, config.model_version)

        # Calibration first pass needs a provisional confidence estimate;
        # score once with raw probability, then apply calibration, then
        # rescore with the calibrated probability so confidence reflects it.
        candidate.model_version = candidate.model_version or config.model_version
        score_candidate(
            candidate, config,
            historical_market_win_rate=historical["market_win_rate"],
            historical_market_roi=historical["market_roi"],
            historical_model_version_win_rate=historical["model_win_rate"],
            historical_model_version_roi=historical["model_roi"],
            calibration_quality=0.5,
        )

        if calibration_buckets:
            calib = calibrate_probability(
                candidate.model_probability, candidate.confidence_score, calibration_buckets, config
            )
            candidate.calibrated_probability = calib.calibrated_probability
            candidate.calibration_sample_size = calib.calibration_sample_size
            candidate.calibration_is_preliminary = calib.is_preliminary
            if not calib.is_preliminary and abs(calib.calibrated_probability - candidate.model_probability) > 1e-9:
                original_probability = candidate.model_probability
                candidate.model_probability = calib.calibrated_probability
                score_candidate(
                    candidate, config,
                    historical_market_win_rate=historical["market_win_rate"],
                    historical_market_roi=historical["market_roi"],
                    historical_model_version_win_rate=historical["model_win_rate"],
                    historical_model_version_roi=historical["model_roi"],
                    calibration_quality=1.0 - config.calibration_max_adjustment,
                )
                candidate.model_probability = original_probability
                candidate.calibrated_probability = calib.calibrated_probability
        else:
            candidate.calibrated_probability = candidate.model_probability
            candidate.calibration_is_preliminary = True

        scored.append(candidate)

    passed, rejected = filter_candidates(scored, config, now=now)

    for candidate in passed:
        candidate.selection_score = compute_selection_score(
            candidate, correlation_penalty_applied=False, weights=config.ranking_weights
        )
    passed.sort(key=lambda c: (c.selection_score or 0.0), reverse=True)

    high_risk_or_insufficient: List[CandidatePrediction] = []
    diversification_eligible: List[CandidatePrediction] = []
    for candidate in passed:
        prelim_group = _assign_group(candidate, config, is_diversified_out=False)
        if prelim_group in (GROUP_HIGH_RISK, GROUP_INSUFFICIENT_DATA):
            candidate.recommendation_group = prelim_group
            high_risk_or_insufficient.append(candidate)
        else:
            diversification_eligible.append(candidate)

    main_pool_target = config.max_main_recommendations + config.max_reserve_recommendations
    kept, dropped_for_diversity = diversify(diversification_eligible, config, max_picks=main_pool_target)

    result = SelectionResult(generated_at=now.isoformat(), total_candidates_considered=total_considered)

    main_count = 0
    for candidate in kept:
        if main_count < config.max_main_recommendations and \
                candidate.confidence_score is not None and \
                candidate.confidence_score >= config.preferred_confidence_score and \
                candidate.odds <= config.max_decimal_odds_main:
            candidate.recommendation_group = GROUP_MAIN
            result.main.append(candidate)
            main_count += 1
        elif len(result.reserve) < config.max_reserve_recommendations:
            candidate.recommendation_group = GROUP_RESERVE
            result.reserve.append(candidate)
        else:
            candidate.recommendation_group = GROUP_RESERVE
            dropped_for_diversity.append(candidate)

    for candidate in dropped_for_diversity:
        if len(result.reserve) < config.max_reserve_recommendations:
            candidate.recommendation_group = GROUP_RESERVE
            result.reserve.append(candidate)

    for candidate in high_risk_or_insufficient:
        if candidate.recommendation_group == GROUP_HIGH_RISK:
            if len(result.high_risk) < config.max_high_risk:
                result.high_risk.append(candidate)
        else:
            result.insufficient_data.append(candidate)

    for candidate in rejected:
        if candidate.confidence_score is not None and candidate.confidence_score < config.min_confidence_score:
            candidate.recommendation_group = GROUP_AVOID
            result.avoid.append(candidate)
    result.avoid.sort(key=lambda c: (c.confidence_score or 0.0), reverse=True)
    result.avoid = result.avoid[: config.max_avoid_shown]
    result.rejected = rejected

    if not result.main:
        reasons = []
        if total_considered == 0:
            reasons.append("Нет доступных кандидатов для анализа на сегодня.")
        else:
            if not passed:
                reasons.append(
                    "Ни один из проанализированных исходов не прошёл минимальные фильтры "
                    "(коэффициенты, полнота данных, отрицательная ценность и т.д.)."
                )
            elif not kept:
                reasons.append(
                    "Прошедшие фильтры исходы были отсеяны правилами диверсификации "
                    "(слишком похожие рынки/события)."
                )
            else:
                reasons.append(
                    f"Ни один исход не достиг требуемого уровня уверенности "
                    f"({config.preferred_confidence_score:.0f}+) при коэффициенте "
                    f"не выше {config.max_decimal_odds_main:.2f}."
                )
            reasons.append("Система не добавляет слабые прогнозы искусственно, чтобы заполнить квоту.")
        result.no_recommendation_reasons = reasons

    return result
