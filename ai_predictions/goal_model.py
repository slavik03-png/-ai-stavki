"""
Minimal, transparent independent-Poisson goal model.

Used only to turn two real, retrieved numbers -- a team's average goals
scored and its opponent's average goals conceded over real recent
matches -- into an honest probability for a totals/BTTS market when
API-Football's own `/predictions` percentages do not cover that market
(they only cover the match winner, not total-goals lines). This is a
standard, well-understood statistical technique (sum of two independent
Poisson variables is itself Poisson with the summed rate) -- never a
fabricated number, and never presented with more precision than the
inputs support (see ai_predictions/football_predictions.py for rounding
and completeness handling).

No external dependency (no scipy/numpy) -- the whole model is a dozen
lines of arithmetic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k) for X ~ Poisson(lam)."""
    return sum(poisson_pmf(i, lam) for i in range(0, k + 1))


@dataclass
class GoalMarketProbabilities:
    over_1_5: float
    over_2_5: float
    under_3_5: float
    btts_yes: float
    btts_no: float


def estimate_total_goals_probabilities(expected_home_goals: float, expected_away_goals: float) -> GoalMarketProbabilities:
    """`expected_home_goals`/`expected_away_goals` must already be real,
    non-negative averages derived from retrieved match data (never
    invented here). Total goals in the match is modelled as
    Poisson(expected_home_goals + expected_away_goals) (independence
    assumption -- the standard simplification for this kind of model);
    BTTS uses the two goal counts independently."""
    lam_home = max(0.0, expected_home_goals)
    lam_away = max(0.0, expected_away_goals)
    lam_total = lam_home + lam_away

    over_1_5 = 1.0 - poisson_cdf(1, lam_total)
    over_2_5 = 1.0 - poisson_cdf(2, lam_total)
    under_3_5 = poisson_cdf(3, lam_total)

    p_home_zero = poisson_pmf(0, lam_home)
    p_away_zero = poisson_pmf(0, lam_away)
    btts_no = p_home_zero + p_away_zero - (p_home_zero * p_away_zero)
    btts_yes = 1.0 - btts_no

    return GoalMarketProbabilities(
        over_1_5=over_1_5, over_2_5=over_2_5, under_3_5=under_3_5,
        btts_yes=btts_yes, btts_no=btts_no,
    )
