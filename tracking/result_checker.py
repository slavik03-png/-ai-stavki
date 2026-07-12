"""
Provider-independent result-checking architecture.

`ResultProvider` is the abstract contract any real football-results source
must implement to plug into automatic settlement. `MockResultProvider` is a
deterministic, in-memory stand-in used by tests -- it makes no network
calls.

`run_settlement_cycle` ties storage + a provider + the settlement engine
together: it finds pending predictions whose events have started, asks the
provider for a result, settles what it can, and leaves everything else
pending. Because `TrackingStorage.update_prediction_settlement` only writes
when the prediction is still `pending`, running this function repeatedly
(or concurrently) can never settle the same prediction twice.
"""

from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

from tracking.models import EventResult, STATUS_PENDING
from tracking.settlement import settle_prediction
from tracking.storage import TrackingStorage, row_to_event_result


class ResultProvider(ABC):
    """Abstract contract for a source of real event results."""

    #: Short machine-readable identifier, e.g. "mock", "api_football".
    name: str = "base"

    @abstractmethod
    def get_event_result(self, event_id: str) -> Optional[EventResult]:
        """Returns the current result for `event_id`, or None if the event
        has not started / no data is available yet. Must never fabricate
        values -- unknown fields on the returned EventResult stay None."""


class MockResultProvider(ResultProvider):
    """Deterministic in-memory result provider for tests. Results are
    supplied up front; no network access of any kind."""

    name = "mock"

    def __init__(self, results: Optional[Dict[str, EventResult]] = None):
        self._results = dict(results or {})

    def set_result(self, event_id: str, result: EventResult) -> None:
        self._results[event_id] = result

    def get_event_result(self, event_id: str) -> Optional[EventResult]:
        return self._results.get(event_id)


@dataclass
class SettlementCycleReport:
    checked: int = 0
    settled: int = 0
    skipped_no_result: int = 0
    skipped_not_pending: int = 0
    errors: "List[str]" = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def run_settlement_cycle(
    storage: TrackingStorage,
    provider: ResultProvider,
    now: Optional[datetime.datetime] = None,
    sport: Optional[str] = None,
) -> SettlementCycleReport:
    """Settles every pending prediction whose event has started and for
    which the provider has a result. Safe to call repeatedly."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    report = SettlementCycleReport()

    pending = storage.get_pending_predictions(sport=sport)
    for row in pending:
        try:
            start_time = datetime.datetime.fromisoformat(row["event_start_time"])
        except ValueError:
            continue
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=datetime.timezone.utc)
        if start_time > now:
            continue  # event hasn't started yet, nothing to check

        report.checked += 1
        result = provider.get_event_result(row["event_id"])
        if result is None:
            report.skipped_no_result += 1
            continue

        storage.save_event_result(result)

        try:
            from tracking.models import Prediction  # local import avoids cycle at module load
            prediction = Prediction(
                sport=row["sport"], country=row["country"], league=row["league"],
                event_id=row["event_id"], event_start_time=row["event_start_time"],
                home_team=row["home_team"], away_team=row["away_team"],
                market_type=row["market_type"], market_name=row["market_name"],
                selection=row["selection"], line=row["line"],
                bookmaker_odds=row["bookmaker_odds"], model_probability=row["model_probability"],
                confidence_score=row["confidence_score"], confidence_level=row["confidence_level"],
                recommendation_group=row["recommendation_group"], explanation=row["explanation"],
                data_provider=row["data_provider"], model_version=row["model_version"],
                prediction_id=row["prediction_id"], created_at=row["created_at"],
                status=row["status"],
            )
            status, explanation = settle_prediction(prediction, result)
        except ValueError as exc:
            report.errors.append(f"{row['prediction_id']}: {exc}")
            continue

        applied = storage.update_prediction_settlement(
            prediction_id=row["prediction_id"],
            status=status,
            final_score=result.final_score,
            first_half_score=result.first_half_score,
            settlement_explanation=explanation,
        )
        if applied:
            report.settled += 1
        else:
            report.skipped_not_pending += 1

    return report
