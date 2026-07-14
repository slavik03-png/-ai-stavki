"""
Thin, failure-isolated hook used by ai_predictions/football_pipeline.py to
permanently record every freshly generated recommendation. Deliberately
tiny and defensive: any error here is caught and logged, never allowed to
break the real prediction pipeline (Telegram cards must always still be
sent even if analytics recording fails for some reason).
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, Optional

from analytics.config import MARKET_LABELS_RU
from analytics.storage import AnalyticsStorage

logger = logging.getLogger(__name__)


def record_recommendation(
    analytics_storage: AnalyticsStorage, rec: Any, odds: Optional[float], *,
    model_version: str, archive_version: str, now: Optional[datetime.datetime] = None,
) -> None:
    """`rec` is a RankedRecommendation from ai_predictions/prediction_selector.py
    (has .candidate and .signal_level); never imported by type here to avoid
    a hard dependency cycle -- only the attributes actually used are read."""
    try:
        c = rec.candidate
        fixture = c.fixture
        row: Dict[str, Any] = {
            "match_datetime": fixture.kickoff_utc.isoformat(),
            "sport": "football",
            "country": fixture.league_country,
            "league": fixture.league_name,
            "fixture_id": fixture.fixture_id,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "market": c.market_key,
            "market_label": MARKET_LABELS_RU.get(c.market_key, c.market_label_ru),
            "selection": c.market_key,
            "odds": odds if odds is not None else (round(1.0 / c.probability, 4) if c.probability > 0 else None),
            "estimated_probability": c.probability,
            "signal_level": rec.signal_level,
            "reason": c.rationale,
            "model_version": model_version,
            "archive_version": archive_version,
            "prediction_source": "api_football" if odds is None else "api_football+the_odds_api",
        }
        analytics_storage.record_prediction(row)
    except Exception:  # never let analytics recording break the real pipeline
        logger.exception("analytics: failed to record recommendation for fixture %r", getattr(rec, "candidate", None))
