"""
Confidence calibration.

Buckets settled `tracking.models.Prediction` rows by their stored
confidence_score, compares predicted probability against actual historical
success rate in that bucket, and (only once enough decisive results have
accumulated) applies a conservative adjustment to a new candidate's model
probability.

Never activates aggressive recalibration on a tiny sample: below
`config.calibration_min_sample` decisive results in the relevant bucket,
calibration is a no-op and the result is marked preliminary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from selection_engine.config import SelectionConfig
from tracking.models import (
    DECISIVE_STATUSES,
    STATUS_HALF_LOST,
    STATUS_HALF_WON,
    STATUS_LOST,
    STATUS_WON,
)


@dataclass
class CalibrationBucketStats:
    label: str
    lower: float
    upper: float
    predicted_avg_probability: Optional[float]
    actual_success_rate: Optional[float]
    settled_decisive_count: int
    is_preliminary: bool


@dataclass
class CalibrationResult:
    raw_probability: float
    calibrated_probability: float
    calibration_sample_size: int
    is_preliminary: bool


def _bucket_bounds(edges: Sequence[float]) -> List["tuple[float, float]"]:
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def _bucket_label(lower: float, upper: float) -> str:
    return f"{int(lower)}-{int(upper)}"


def build_calibration_buckets(
    predictions: Sequence,
    config: SelectionConfig,
) -> Dict[str, CalibrationBucketStats]:
    """`predictions` are sqlite3.Row-like objects from
    `tracking.storage.TrackingStorage.list_all_predictions()` (must expose
    `confidence_score`, `model_probability`, and `status`). Only decisive,
    settled predictions (won/lost/half_won/half_lost) contribute -- pending,
    returned, cancelled, postponed and unresolved rows carry no calibration
    signal and are excluded, matching the tracking package's own rule that
    pushes/cancellations never count as wins or losses."""
    bounds = _bucket_bounds(config.calibration_buckets)
    buckets: Dict[str, Dict] = {
        _bucket_label(lo, hi): {"lo": lo, "hi": hi, "probs": [], "success": 0.0, "count": 0}
        for lo, hi in bounds
    }

    for row in predictions:
        status = row["status"]
        if status not in DECISIVE_STATUSES:
            continue
        confidence = row["confidence_score"]
        for label, bucket in buckets.items():
            lo, hi = bucket["lo"], bucket["hi"]
            in_bucket = lo <= confidence < hi or (hi == config.calibration_buckets[-1] and confidence == hi)
            if not in_bucket:
                continue
            bucket["probs"].append(row["model_probability"])
            bucket["count"] += 1
            if status == STATUS_WON:
                bucket["success"] += 1.0
            elif status == STATUS_HALF_WON:
                bucket["success"] += 0.5
            # lost / half_lost contribute 0
            break

    results: Dict[str, CalibrationBucketStats] = {}
    for label, bucket in buckets.items():
        count = bucket["count"]
        predicted_avg = (sum(bucket["probs"]) / count) if count else None
        actual_rate = (bucket["success"] / count) if count else None
        results[label] = CalibrationBucketStats(
            label=label,
            lower=bucket["lo"],
            upper=bucket["hi"],
            predicted_avg_probability=predicted_avg,
            actual_success_rate=actual_rate,
            settled_decisive_count=count,
            is_preliminary=count < config.calibration_min_sample,
        )
    return results


def calibrate_probability(
    model_probability: float,
    confidence_score: float,
    buckets: Dict[str, CalibrationBucketStats],
    config: SelectionConfig,
) -> CalibrationResult:
    """Applies a conservative calibration adjustment based on the bucket
    matching `confidence_score`. Returns raw + calibrated probability, the
    sample size behind the adjustment, and whether it is preliminary."""
    matching: Optional[CalibrationBucketStats] = None
    for bucket in buckets.values():
        if bucket.lower <= confidence_score < bucket.upper or (
            bucket.upper == config.calibration_buckets[-1] and confidence_score == bucket.upper
        ):
            matching = bucket
            break

    if matching is None or matching.is_preliminary or matching.predicted_avg_probability is None \
            or matching.actual_success_rate is None:
        sample = matching.settled_decisive_count if matching else 0
        return CalibrationResult(
            raw_probability=model_probability,
            calibrated_probability=model_probability,
            calibration_sample_size=sample,
            is_preliminary=True,
        )

    # Conservative adjustment: shift the model probability toward the
    # observed bucket-level miscalibration (predicted vs actual), capped at
    # config.calibration_max_adjustment, and only applied at full strength
    # once the sample comfortably clears the minimum -- linearly scaled in
    # between so an accumulating sample tightens calibration gradually.
    miscalibration = matching.actual_success_rate - matching.predicted_avg_probability
    scale = min(1.0, matching.settled_decisive_count / (config.calibration_min_sample * 2.0))
    adjustment = max(
        -config.calibration_max_adjustment,
        min(config.calibration_max_adjustment, miscalibration * scale),
    )
    calibrated = max(0.0, min(1.0, model_probability + adjustment))
    return CalibrationResult(
        raw_probability=model_probability,
        calibrated_probability=calibrated,
        calibration_sample_size=matching.settled_decisive_count,
        is_preliminary=False,
    )
