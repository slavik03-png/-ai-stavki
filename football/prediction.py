"""
Football analytics / prediction engine -- first version.

Analyzes only statistics returned through a `FootballStatisticsProvider`
(interface.py). Never invents values: whenever a statistic is missing or
unavailable, the affected market's confidence is reduced and the gap is
recorded in `missing_statistics` -- it is never silently treated as zero.

All calculations are deterministic (no randomness): the same provider data
always produces the same MarketResult objects.

This module has no dependency on bot.py and is not imported by it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from football.interface import (
    FootballStatisticsProvider,
    MatchSummary,
    Stat,
)

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

STATUS_RECOMMENDED = "recommended"
STATUS_SECONDARY = "secondary"
STATUS_WEAK = "weak"
STATUS_UNAVAILABLE = "unavailable"


@dataclass
class MarketResult:
    market_name: str
    market_type: str
    confidence: float  # always 0-100; 0 only ever means "no usable evidence"
    strength: str
    risk: str
    stars: int  # always 1-5
    explanation: List[str]
    supporting_statistics: List[str]
    missing_statistics: List[str]
    status: str  # recommended | secondary | weak | unavailable
    # Internal grouping key used by recommendation.py to avoid near-duplicate
    # markets dominating the report. Not part of the externally required
    # fields, but harmless to carry along.
    family: str = ""


# ---------------------------------------------------------------------------
# Match context: every Stat needed by the engine, fetched once
# ---------------------------------------------------------------------------

@dataclass
class MatchContext:
    provider_name: str
    home_team: str
    away_team: str
    league: Optional[str]

    home_form: Stat
    away_form: Stat
    home_last: Stat
    away_last: Stat
    h2h: Stat

    home_goals_half: Stat
    away_goals_half: Stat
    home_btts: Stat
    away_btts: Stat
    home_clean: Stat
    away_clean: Stat
    home_corners: Stat
    away_corners: Stat
    home_fouls: Stat
    away_fouls: Stat
    home_cards: Stat
    away_cards: Stat
    home_shots: Stat
    away_shots: Stat

    standings: Stat
    lineups: Stat
    home_injuries: Stat
    away_injuries: Stat


def gather_match_context(
    provider: FootballStatisticsProvider,
    home_team: str,
    away_team: str,
    league: Optional[str] = None,
    count: int = 10,
) -> MatchContext:
    """Fetches every statistic the engine can use, exactly once per match."""
    standings = provider.get_standings(league) if league else Stat.missing("Лига не указана")
    return MatchContext(
        provider_name=provider.name,
        home_team=home_team,
        away_team=away_team,
        league=league,
        home_form=provider.get_home_away_form(home_team, count),
        away_form=provider.get_home_away_form(away_team, count),
        home_last=provider.get_last_matches(home_team, count),
        away_last=provider.get_last_matches(away_team, count),
        h2h=provider.get_head_to_head(home_team, away_team, count),
        home_goals_half=provider.get_goals_by_half(home_team, count),
        away_goals_half=provider.get_goals_by_half(away_team, count),
        home_btts=provider.get_btts_frequency(home_team, count),
        away_btts=provider.get_btts_frequency(away_team, count),
        home_clean=provider.get_clean_sheets(home_team, count),
        away_clean=provider.get_clean_sheets(away_team, count),
        home_corners=provider.get_corners(home_team, count),
        away_corners=provider.get_corners(away_team, count),
        home_fouls=provider.get_fouls(home_team, count),
        away_fouls=provider.get_fouls(away_team, count),
        home_cards=provider.get_cards(home_team, count),
        away_cards=provider.get_cards(away_team, count),
        home_shots=provider.get_shots(home_team, count),
        away_shots=provider.get_shots(away_team, count),
        standings=standings,
        lineups=provider.get_lineups(home_team, away_team),
        home_injuries=provider.get_injuries(home_team),
        away_injuries=provider.get_injuries(away_team),
    )


# ---------------------------------------------------------------------------
# Shared math helpers -- deterministic, no randomness
# ---------------------------------------------------------------------------

@dataclass
class Fraction:
    count: int
    total: int

    @property
    def ratio(self) -> Optional[float]:
        return self.count / self.total if self.total else None


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _sample_factor(total: int, target: int = 10) -> float:
    if total <= 0:
        return 0.0
    return min(1.0, total / target)


def _is_contradictory(fractions: List[Fraction], threshold: float = 0.4) -> bool:
    valid = [f.ratio for f in fractions if f.total >= 3 and f.ratio is not None]
    if len(valid) < 2:
        return False
    return (max(valid) - min(valid)) > threshold


#: Weight/ratio used to represent a factor that is intended to matter for a
#: market but whose real data is missing. It nudges the blended average back
#: toward neutral (never toward "confirmed") instead of silently dropping the
#: factor -- dropping it outright can perversely raise confidence when the
#: missing factor would likely have pulled the number down. This is a
#: calibration constant, not a fabricated statistic: it is never shown to the
#: user as a real value, only used internally to keep confidence honest.
_NEUTRAL_RATIO = 0.5
_NEUTRAL_WEIGHT = 5.0


def compute_confidence(
    fractions: List[Optional[Fraction]],
    missing_count: int,
    h2h_adjustment: float = 0.0,
    sample_target: int = 10,
) -> Tuple[Optional[float], bool]:
    """
    Blends a list of Fractions (sample-weighted average ratio) into a 0-100
    confidence score, then applies sample-size, missing-data, and
    contradiction penalties. Returns (confidence_or_None, contradiction_flag).
    confidence is None only when there is no usable evidence at all (every
    entry is None or has zero sample size).

    Entries that are None (or have total==0) represent an intended factor
    with no real data -- they are blended in at a neutral weight rather than
    dropped, so removing bad/negative real evidence can never look like an
    improvement over having it.
    """
    real = [f for f in fractions if f is not None and f.total > 0]
    if not real:
        return None, False

    weighted_sum = 0.0
    total_weight = 0.0
    for f in fractions:
        if f is not None and f.total > 0:
            weighted_sum += (f.ratio or 0.0) * f.total
            total_weight += f.total
        else:
            weighted_sum += _NEUTRAL_RATIO * _NEUTRAL_WEIGHT
            total_weight += _NEUTRAL_WEIGHT

    weighted_ratio = weighted_sum / total_weight
    raw_confidence = weighted_ratio * 100.0

    sample_factor = _sample_factor(total_weight, sample_target)
    confidence = raw_confidence * (0.5 + 0.5 * sample_factor)

    contradiction = _is_contradictory(real)
    confidence -= missing_count * 8.0
    if contradiction:
        confidence -= 12.0
    confidence += h2h_adjustment

    return _clamp(confidence), contradiction


def _strength_label(confidence: float) -> str:
    if confidence >= 85:
        return "очень высокая"
    if confidence >= 75:
        return "высокая"
    if confidence >= 65:
        return "средняя"
    if confidence >= 55:
        return "низкая"
    return "минимальная"


def _risk_label(confidence: float, missing_count: int, contradiction: bool) -> str:
    if confidence < 55 or contradiction or missing_count >= 3:
        return "высокий"
    if confidence >= 75 and missing_count == 0 and not contradiction:
        return "низкий"
    return "средний"


def _stars_from_confidence(confidence: float) -> int:
    if confidence >= 85:
        return 5
    if confidence >= 70:
        return 4
    if confidence >= 55:
        return 3
    if confidence >= 40:
        return 2
    return 1


def _status_from_confidence(confidence: Optional[float]) -> str:
    if confidence is None:
        return STATUS_UNAVAILABLE
    if confidence >= 65:
        return STATUS_RECOMMENDED
    if confidence >= 55:
        return STATUS_SECONDARY
    return STATUS_WEAK


def _build_unavailable(market_name: str, market_type: str, family: str, missing: List[str]) -> MarketResult:
    return MarketResult(
        market_name=market_name,
        market_type=market_type,
        confidence=0.0,
        strength="нет данных",
        risk="высокий",
        stars=1,
        explanation=[],
        supporting_statistics=[],
        missing_statistics=missing,
        status=STATUS_UNAVAILABLE,
        family=family,
    )


def _finalize(
    market_name: str,
    market_type: str,
    family: str,
    confidence: Optional[float],
    contradiction: bool,
    missing: List[str],
    explanation: List[str],
    supporting: List[str],
) -> MarketResult:
    if confidence is None:
        return _build_unavailable(market_name, market_type, family, missing)
    missing_count = len(missing)
    if contradiction:
        explanation = explanation + ["домашняя и выездная тенденции команд расходятся"]
    return MarketResult(
        market_name=market_name,
        market_type=market_type,
        confidence=round(confidence, 1),
        strength=_strength_label(confidence),
        risk=_risk_label(confidence, missing_count, contradiction),
        stars=_stars_from_confidence(confidence),
        explanation=explanation,
        supporting_statistics=supporting,
        missing_statistics=missing,
        status=_status_from_confidence(confidence),
        family=family,
    )


# ---------------------------------------------------------------------------
# Match-summary helpers
# ---------------------------------------------------------------------------

def _team_perspective(match: MatchSummary, team: str) -> Optional[Tuple[int, int, bool]]:
    if match.home_team == team and match.home_goals is not None and match.away_goals is not None:
        return match.home_goals, match.away_goals, True
    if match.away_team == team and match.home_goals is not None and match.away_goals is not None:
        return match.away_goals, match.home_goals, False
    return None


def _team_half_perspective(match: MatchSummary, team: str) -> Optional[Tuple[int, int, int, int]]:
    """Returns (ht_scored, ht_conceded, sh_scored, sh_conceded) for `team`."""
    result = _team_perspective(match, team)
    if result is None:
        return None
    ft_scored, ft_conceded, is_home = result
    ht_scored = match.ht_home_goals if is_home else match.ht_away_goals
    ht_conceded = match.ht_away_goals if is_home else match.ht_home_goals
    if ht_scored is None or ht_conceded is None:
        return None
    return ht_scored, ht_conceded, ft_scored - ht_scored, ft_conceded - ht_conceded


def _match_half_totals(match: MatchSummary) -> Optional[Tuple[int, int]]:
    """Returns (first_half_total_goals, second_half_total_goals) for the match."""
    if match.ht_home_goals is None or match.ht_away_goals is None:
        return None
    if match.home_goals is None or match.away_goals is None:
        return None
    ht_total = match.ht_home_goals + match.ht_away_goals
    ft_total = match.home_goals + match.away_goals
    return ht_total, ft_total - ht_total


def _team_matches(stat: Stat) -> List[MatchSummary]:
    return stat.value if stat.available and stat.value else []


def _fraction_team_total_over(matches: List[MatchSummary], team: str, threshold: float) -> Fraction:
    """Fraction of `team`'s matches where the MATCH total goals > threshold."""
    count, total = 0, 0
    for m in matches:
        result = _team_perspective(m, team)
        if result is None:
            continue
        scored, conceded, _ = result
        total += 1
        if (scored + conceded) > threshold:
            count += 1
    return Fraction(count, total)


def _fraction_team_scored_over(matches: List[MatchSummary], team: str, threshold: float) -> Fraction:
    """Fraction of `team`'s matches where `team` itself scored > threshold goals."""
    count, total = 0, 0
    for m in matches:
        result = _team_perspective(m, team)
        if result is None:
            continue
        scored, _, _ = result
        total += 1
        if scored > threshold:
            count += 1
    return Fraction(count, total)


def _fraction_match_half_over(matches: List[MatchSummary], half: str, threshold: float) -> Fraction:
    """Fraction of matches where the given half's total goals > threshold. half is 'first' or 'second'."""
    count, total = 0, 0
    for m in matches:
        totals = _match_half_totals(m)
        if totals is None:
            continue
        first, second = totals
        value = first if half == "first" else second
        total += 1
        if value > threshold:
            count += 1
    return Fraction(count, total)


def _fraction_team_scored_in_half(matches: List[MatchSummary], team: str, half: str) -> Fraction:
    count, total = 0, 0
    for m in matches:
        splits = _team_half_perspective(m, team)
        if splits is None:
            continue
        ht_scored, _, sh_scored, _ = splits
        total += 1
        scored = ht_scored if half == "first" else sh_scored
        if scored > 0:
            count += 1
    return Fraction(count, total)


def _fraction_goal_in_both_halves(matches: List[MatchSummary]) -> Fraction:
    count, total = 0, 0
    for m in matches:
        totals = _match_half_totals(m)
        if totals is None:
            continue
        first, second = totals
        total += 1
        if first > 0 and second > 0:
            count += 1
    return Fraction(count, total)


def _btts_fraction_from_stat(stat: Stat) -> Optional[Fraction]:
    if not stat.available or not stat.value:
        return None
    try:
        count_s, total_s = stat.value.split("/")
        return Fraction(int(count_s), int(total_s))
    except (ValueError, AttributeError):
        return None


def _form_win_rate(form_string: Optional[str], letter: str) -> Optional[Fraction]:
    if not form_string:
        return None
    total = len(form_string)
    if total == 0:
        return None
    return Fraction(form_string.count(letter), total)


def _h2h_adjustment_for_home(h2h: Stat, home_team: str) -> float:
    """
    Small, capped adjustment (max +-5 points) based on head-to-head results.
    H2H is intentionally never the main factor for any market.
    """
    if not h2h.available or not h2h.value:
        return 0.0
    matches = h2h.value
    home_wins = draws = away_wins = 0
    for m in matches:
        result = _team_perspective(m, home_team)
        if result is None:
            continue
        scored, conceded, _ = result
        if scored > conceded:
            home_wins += 1
        elif scored < conceded:
            away_wins += 1
        else:
            draws += 1
    total = home_wins + draws + away_wins
    if total == 0:
        return 0.0
    net = (home_wins - away_wins) / total  # -1..1
    return _clamp(net * 5.0, -5.0, 5.0)


# ---------------------------------------------------------------------------
# Market group: match result
# ---------------------------------------------------------------------------

def _match_result_markets(ctx: MatchContext) -> List[MarketResult]:
    results: List[MarketResult] = []
    missing: List[str] = []

    home_overall = ctx.home_form.value if ctx.home_form.available else None
    away_overall = ctx.away_form.value if ctx.away_form.available else None
    if not ctx.home_form.available:
        missing.append(f"нет данных о форме {ctx.home_team}")
    if not ctx.away_form.available:
        missing.append(f"нет данных о форме {ctx.away_team}")

    home_home_win = _form_win_rate(home_overall.home if home_overall else None, "W")
    home_home_loss = _form_win_rate(home_overall.home if home_overall else None, "L")
    home_home_draw = _form_win_rate(home_overall.home if home_overall else None, "D")
    away_away_win = _form_win_rate(away_overall.away if away_overall else None, "W")
    away_away_loss = _form_win_rate(away_overall.away if away_overall else None, "L")
    away_away_draw = _form_win_rate(away_overall.away if away_overall else None, "D")

    h2h_adj = _h2h_adjustment_for_home(ctx.h2h, ctx.home_team)
    h2h_missing = [] if ctx.h2h.available else [f"нет данных личных встреч: {ctx.h2h.reason}"]

    def build(name: str, fam: str, fractions: List[Optional[Fraction]], support: List[str],
              h2h_adj_signed: float) -> MarketResult:
        local_missing = list(missing) + h2h_missing
        conf, contradiction = compute_confidence(fractions, len(local_missing), h2h_adj_signed)
        explanation = []
        supporting = []
        if conf is not None:
            for label, frac in zip(support, fractions):
                if frac is not None and frac.total:
                    explanation.append(f"{label}: {frac.count}/{frac.total}")
                    supporting.append(label)
            if ctx.h2h.available and h2h_adj_signed != 0.0:
                explanation.append("личные встречи учтены как второстепенный фактор")
        return _finalize(name, "match_result", fam, conf, contradiction, local_missing, explanation,
                          supporting)

    results.append(build(
        "Победа хозяев", "home_win",
        [home_home_win, away_away_loss],
        [f"{ctx.home_team}: победы дома", f"{ctx.away_team}: поражения в гостях"],
        h2h_adj,
    ))
    results.append(build(
        "Ничья", "draw",
        [home_home_draw, away_away_draw],
        [f"{ctx.home_team}: ничьи дома", f"{ctx.away_team}: ничьи в гостях"],
        0.0,
    ))
    results.append(build(
        "Победа гостей", "away_win",
        [away_away_win, home_home_loss],
        [f"{ctx.away_team}: победы в гостях", f"{ctx.home_team}: поражения дома"],
        -h2h_adj,
    ))

    # Double chance: derived from the two related outright markets, capped.
    home_win_conf = results[0].confidence if results[0].status != STATUS_UNAVAILABLE else None
    draw_conf = results[1].confidence if results[1].status != STATUS_UNAVAILABLE else None
    away_win_conf = results[2].confidence if results[2].status != STATUS_UNAVAILABLE else None

    def double_chance(name: str, fam: str, conf_a: Optional[float], conf_b: Optional[float],
                       label: str) -> MarketResult:
        if conf_a is None and conf_b is None:
            return _build_unavailable(name, "match_result", fam, list(missing) + h2h_missing)
        best = max(v for v in (conf_a, conf_b) if v is not None)
        conf = _clamp(best + 10.0)
        explanation = [f"объединяет два исхода: {label}", "покрытие двух исходов увеличивает уверенность"]
        return _finalize(name, "match_result", fam, conf, False, list(missing) + h2h_missing, explanation,
                          [label])

    results.append(double_chance("Двойной шанс 1X", "double_chance_1x", home_win_conf, draw_conf,
                                  "победа хозяев или ничья"))
    results.append(double_chance("Двойной шанс X2", "double_chance_x2", draw_conf, away_win_conf,
                                  "ничья или победа гостей"))
    results.append(double_chance("Двойной шанс 12", "double_chance_12", home_win_conf, away_win_conf,
                                  "победа хозяев или победа гостей"))

    return results


# ---------------------------------------------------------------------------
# Market group: goals
# ---------------------------------------------------------------------------

def _goals_markets(ctx: MatchContext) -> List[MarketResult]:
    results: List[MarketResult] = []
    home_matches = _team_matches(ctx.home_last)
    away_matches = _team_matches(ctx.away_last)
    missing: List[str] = []
    if not ctx.home_last.available:
        missing.append(f"нет последних матчей {ctx.home_team}: {ctx.home_last.reason}")
    if not ctx.away_last.available:
        missing.append(f"нет последних матчей {ctx.away_team}: {ctx.away_last.reason}")

    def over_under_market(name: str, fam: str, threshold: float, home_frac_fn, away_frac_fn,
                           extra_support: str) -> MarketResult:
        home_f = home_frac_fn(home_matches, ctx.home_team, threshold) if home_matches else Fraction(0, 0)
        away_f = away_frac_fn(away_matches, ctx.away_team, threshold) if away_matches else Fraction(0, 0)
        conf, contradiction = compute_confidence([home_f, away_f], len(missing))
        explanation = []
        if conf is not None:
            explanation.append(f"{ctx.home_team}: {extra_support} {home_f.count}/{home_f.total} матчей")
            explanation.append(f"{ctx.away_team}: {extra_support} {away_f.count}/{away_f.total} матчей")
            explanation.append("оценка основана на собственных последних матчах каждой команды, а не именно на этой паре")
        return _finalize(name, "goals", fam, conf, contradiction, missing,
                          explanation, [extra_support])

    results.append(over_under_market("Тотал больше 0.5", "goals_over", 0.5,
                                      _fraction_team_total_over, _fraction_team_total_over, "матчей с тоталом >0.5"))
    results.append(over_under_market("Тотал больше 1.5", "goals_over", 1.5,
                                      _fraction_team_total_over, _fraction_team_total_over, "матчей с тоталом >1.5"))
    results.append(over_under_market("Тотал больше 2.5", "goals_over", 2.5,
                                      _fraction_team_total_over, _fraction_team_total_over, "матчей с тоталом >2.5"))
    results.append(over_under_market("Тотал больше 3.5", "goals_over", 3.5,
                                      _fraction_team_total_over, _fraction_team_total_over, "матчей с тоталом >3.5"))

    # Under 2.5 uses the complement of the same underlying data (not invented,
    # just the inverse frequency of the same real matches).
    def under_market(name: str, fam: str, threshold: float) -> MarketResult:
        def frac_under(matches, team, thr):
            f = _fraction_team_total_over(matches, team, thr)
            return Fraction(f.total - f.count, f.total)
        home_f = frac_under(home_matches, ctx.home_team, threshold) if home_matches else Fraction(0, 0)
        away_f = frac_under(away_matches, ctx.away_team, threshold) if away_matches else Fraction(0, 0)
        conf, contradiction = compute_confidence([home_f, away_f], len(missing))
        explanation = []
        if conf is not None:
            explanation.append(f"{ctx.home_team}: матчей с тоталом <={threshold}: {home_f.count}/{home_f.total}")
            explanation.append(f"{ctx.away_team}: матчей с тоталом <={threshold}: {away_f.count}/{away_f.total}")
        return _finalize(name, "goals", fam, conf, contradiction, missing, explanation, ["тотал матчей"])

    results.append(under_market("Тотал меньше 2.5", "goals_under", 2.5))

    # BTTS
    home_btts = _btts_fraction_from_stat(ctx.home_btts)
    away_btts = _btts_fraction_from_stat(ctx.away_btts)
    btts_missing = list(missing)
    if not ctx.home_btts.available:
        btts_missing.append(f"нет статистики BTTS для {ctx.home_team}: {ctx.home_btts.reason}")
    if not ctx.away_btts.available:
        btts_missing.append(f"нет статистики BTTS для {ctx.away_team}: {ctx.away_btts.reason}")
    btts_fracs = [f for f in (home_btts, away_btts) if f is not None]
    conf, contradiction = compute_confidence(btts_fracs, len(btts_missing))
    explanation = []
    if conf is not None:
        if home_btts:
            explanation.append(f"{ctx.home_team}: обе забили в {home_btts.count}/{home_btts.total} матчах")
        if away_btts:
            explanation.append(f"{ctx.away_team}: обе забили в {away_btts.count}/{away_btts.total} матчах")
    results.append(_finalize("Обе забьют — Да", "goals", "btts_yes", conf, contradiction, btts_missing,
                              explanation, ["частота BTTS"]))

    # BTTS No: derived from clean sheets / failed to score (opponent's perspective)
    clean_missing = list(missing)
    home_clean = ctx.home_clean.value if ctx.home_clean.available else None
    away_clean = ctx.away_clean.value if ctx.away_clean.available else None
    if not ctx.home_clean.available:
        clean_missing.append(f"нет статистики сухих матчей {ctx.home_team}: {ctx.home_clean.reason}")
    if not ctx.away_clean.available:
        clean_missing.append(f"нет статистики сухих матчей {ctx.away_team}: {ctx.away_clean.reason}")
    no_btts_fracs = []
    explanation = []
    if home_clean:
        f = Fraction(home_clean.clean_sheets, home_clean.matches_counted)
        no_btts_fracs.append(f)
        explanation.append(f"{ctx.home_team}: сухие матчи {f.count}/{f.total}")
    if away_clean:
        f = Fraction(away_clean.failed_to_score, away_clean.matches_counted)
        no_btts_fracs.append(f)
        explanation.append(f"{ctx.away_team}: не забили {f.count}/{f.total} матчей")
    conf, contradiction = compute_confidence(no_btts_fracs, len(clean_missing))
    results.append(_finalize("Обе забьют — Нет", "goals", "btts_no", conf, contradiction, clean_missing,
                              explanation if conf is not None else [], ["сухие матчи / не забили"]))

    # Goal in both halves
    both_halves_missing = list(missing)
    home_bh = _fraction_goal_in_both_halves(home_matches) if home_matches else Fraction(0, 0)
    away_bh = _fraction_goal_in_both_halves(away_matches) if away_matches else Fraction(0, 0)
    conf, contradiction = compute_confidence([home_bh, away_bh], len(both_halves_missing))
    explanation = []
    if conf is not None:
        explanation.append(f"{ctx.home_team}: гол в каждом тайме в {home_bh.count}/{home_bh.total} матчах")
        explanation.append(f"{ctx.away_team}: гол в каждом тайме в {away_bh.count}/{away_bh.total} матчах")
    results.append(_finalize("Гол в обоих таймах", "goals", "goal_both_halves", conf, contradiction,
                              both_halves_missing, explanation, ["гол в каждом тайме"]))

    # Team-specific over markets
    def team_over(name: str, fam: str, team: str, matches: List[MatchSummary], threshold: float,
                  team_missing: List[str]) -> MarketResult:
        f = _fraction_team_scored_over(matches, team, threshold) if matches else Fraction(0, 0)
        conf, contradiction = compute_confidence([f], len(team_missing))
        explanation = [f"{team}: забивал больше {threshold} гол(ов) в {f.count}/{f.total} матчах"] if conf is not None else []
        return _finalize(name, "goals", fam, conf, contradiction, team_missing, explanation, ["голы команды"])

    home_missing = [] if ctx.home_last.available else [f"нет последних матчей {ctx.home_team}"]
    away_missing = [] if ctx.away_last.available else [f"нет последних матчей {ctx.away_team}"]

    results.append(team_over(f"{ctx.home_team}: тотал больше 0.5", "home_team_goals", ctx.home_team,
                              home_matches, 0.5, home_missing))
    results.append(team_over(f"{ctx.home_team}: тотал больше 1.5", "home_team_goals", ctx.home_team,
                              home_matches, 1.5, home_missing))
    results.append(team_over(f"{ctx.away_team}: тотал больше 0.5", "away_team_goals", ctx.away_team,
                              away_matches, 0.5, away_missing))
    results.append(team_over(f"{ctx.away_team}: тотал больше 1.5", "away_team_goals", ctx.away_team,
                              away_matches, 1.5, away_missing))

    return results


# ---------------------------------------------------------------------------
# Market group: first half / second half
# ---------------------------------------------------------------------------

def _half_markets(ctx: MatchContext) -> List[MarketResult]:
    results: List[MarketResult] = []
    home_matches = _team_matches(ctx.home_last)
    away_matches = _team_matches(ctx.away_last)
    missing: List[str] = []
    if not ctx.home_last.available:
        missing.append(f"нет последних матчей {ctx.home_team}")
    if not ctx.away_last.available:
        missing.append(f"нет последних матчей {ctx.away_team}")

    def half_over(name: str, fam: str, half: str, threshold: float) -> MarketResult:
        home_f = _fraction_match_half_over(home_matches, half, threshold) if home_matches else Fraction(0, 0)
        away_f = _fraction_match_half_over(away_matches, half, threshold) if away_matches else Fraction(0, 0)
        conf, contradiction = compute_confidence([home_f, away_f], len(missing))
        explanation = []
        if conf is not None:
            half_ru = "1-й тайм" if half == "first" else "2-й тайм"
            explanation.append(f"{ctx.home_team}: {half_ru} с тоталом >{threshold} в {home_f.count}/{home_f.total} матчах")
            explanation.append(f"{ctx.away_team}: {half_ru} с тоталом >{threshold} в {away_f.count}/{away_f.total} матчах")
        market_type = "first_half" if half == "first" else "second_half"
        return _finalize(name, market_type, fam, conf, contradiction, missing, explanation, ["голы по тайму"])

    results.append(half_over("Первый тайм больше 0.5", "fh_over_05", "first", 0.5))
    results.append(half_over("Первый тайм больше 1.0", "fh_over_10", "first", 1.0))
    results.append(half_over("Второй тайм больше 0.5", "sh_over_05", "second", 0.5))
    results.append(half_over("Второй тайм больше 1.0", "sh_over_10", "second", 1.0))

    def half_btts(name: str, fam: str, half: str) -> MarketResult:
        # First-half BTTS proxy: home team scoring in that half (own matches)
        # combined with away team scoring in that half (own matches).
        home_f = _fraction_team_scored_in_half(home_matches, ctx.home_team, half) if home_matches else Fraction(0, 0)
        away_f = _fraction_team_scored_in_half(away_matches, ctx.away_team, half) if away_matches else Fraction(0, 0)
        conf, contradiction = compute_confidence([home_f, away_f], len(missing))
        explanation = []
        if conf is not None:
            half_ru = "1-й тайм" if half == "first" else "2-й тайм"
            explanation.append(f"{ctx.home_team}: забивал в {half_ru} в {home_f.count}/{home_f.total} матчах")
            explanation.append(f"{ctx.away_team}: забивал в {half_ru} в {away_f.count}/{away_f.total} матчах")
        return _finalize(name, "first_half", fam, conf, contradiction, missing, explanation, ["голы в тайме"])

    results.append(half_btts("Обе забьют в первом тайме", "fh_btts", "first"))

    def team_score_in_half(name: str, fam: str, team: str, matches: List[MatchSummary], half: str,
                            market_type: str) -> MarketResult:
        f = _fraction_team_scored_in_half(matches, team, half) if matches else Fraction(0, 0)
        conf, contradiction = compute_confidence([f], len(missing))
        half_ru = "1-й тайм" if half == "first" else "2-й тайм"
        explanation = [f"{team}: забивал в {half_ru} в {f.count}/{f.total} матчах"] if conf is not None else []
        return _finalize(name, market_type, fam, conf, contradiction, missing, explanation, ["голы в тайме"])

    results.append(team_score_in_half(f"{ctx.home_team} забьёт в первом тайме", "fh_home_score",
                                       ctx.home_team, home_matches, "first", "first_half"))
    results.append(team_score_in_half(f"{ctx.away_team} забьёт в первом тайме", "fh_away_score",
                                       ctx.away_team, away_matches, "first", "first_half"))
    results.append(team_score_in_half(f"{ctx.home_team} забьёт во втором тайме", "sh_home_score",
                                       ctx.home_team, home_matches, "second", "second_half"))
    results.append(team_score_in_half(f"{ctx.away_team} забьёт во втором тайме", "sh_away_score",
                                       ctx.away_team, away_matches, "second", "second_half"))

    return results


# ---------------------------------------------------------------------------
# Market group: additional statistics (corners, cards, fouls, shots)
# ---------------------------------------------------------------------------

def _nearest_half_line(avg: float) -> float:
    """Deterministically picks a betting-style .5 line just below the average."""
    lower = float(int(avg))
    return lower + 0.5 if avg - lower >= 0.5 else max(0.5, lower - 0.5)


def _additional_stats_markets(ctx: MatchContext) -> List[MarketResult]:
    results: List[MarketResult] = []

    def average_over_market(name: str, fam: str, home_stat: Stat, away_stat: Stat, label: str) -> MarketResult:
        missing = []
        if not home_stat.available:
            missing.append(f"нет данных «{label}» для {ctx.home_team}: {home_stat.reason}")
        if not away_stat.available:
            missing.append(f"нет данных «{label}» для {ctx.away_team}: {away_stat.reason}")
        if not home_stat.available and not away_stat.available:
            return _build_unavailable(name, "additional_stats", fam, missing)

        avgs = []
        samples = []
        if home_stat.available:
            avgs.append(home_stat.value.average)
            samples.append(home_stat.value.matches_counted)
        if away_stat.available:
            avgs.append(away_stat.value.average)
            samples.append(away_stat.value.matches_counted)
        combined_avg = sum(avgs)  # match total = sum of both teams' per-match averages
        line = _nearest_half_line(combined_avg)
        sample_total = min(samples) if samples else 0

        # This market is derived from aggregate averages only (no per-match
        # distribution is exposed by the interface), so confidence is
        # deliberately capped below what a full distribution would allow.
        base_confidence = 60.0 if combined_avg > line else 40.0
        sample_f = _sample_factor(sample_total)
        confidence = base_confidence * (0.5 + 0.5 * sample_f)
        confidence -= len(missing) * 8.0
        confidence = _clamp(confidence, 0.0, 75.0)

        explanation = [
            f"ожидаемый суммарный показатель «{label}» за матч: {round(combined_avg, 1)} (линия {line})",
            "оценка основана только на средних показателях, без данных по отдельным матчам",
        ]
        return _finalize(f"{name} {line}", "additional_stats", fam, confidence, False, missing,
                          explanation, [f"средние показатели «{label}»"])

    results.append(average_over_market("Угловые больше", "corners_over", ctx.home_corners, ctx.away_corners, "угловые"))
    results.append(_cards_market(ctx))
    results.append(average_over_market("Фолы больше", "fouls_over", ctx.home_fouls, ctx.away_fouls, "фолы"))
    results.append(_shots_market(ctx, "Удары больше", "shots_over", on_target=False))
    results.append(_shots_market(ctx, "Удары в створ больше", "shots_on_target_over", on_target=True))

    return results


def _cards_market(ctx: MatchContext) -> MarketResult:
    missing = []
    if not ctx.home_cards.available:
        missing.append(f"нет данных о карточках {ctx.home_team}: {ctx.home_cards.reason}")
    if not ctx.away_cards.available:
        missing.append(f"нет данных о карточках {ctx.away_team}: {ctx.away_cards.reason}")
    if not ctx.home_cards.available and not ctx.away_cards.available:
        return _build_unavailable("Карточки больше", "additional_stats", "cards_over", missing)

    combined = 0.0
    samples = []
    if ctx.home_cards.available:
        combined += ctx.home_cards.value.avg_yellow + ctx.home_cards.value.avg_red
        samples.append(ctx.home_cards.value.matches_counted)
    if ctx.away_cards.available:
        combined += ctx.away_cards.value.avg_yellow + ctx.away_cards.value.avg_red
        samples.append(ctx.away_cards.value.matches_counted)
    line = _nearest_half_line(combined)
    sample_total = min(samples) if samples else 0

    base_confidence = 60.0 if combined > line else 40.0
    confidence = base_confidence * (0.5 + 0.5 * _sample_factor(sample_total))
    confidence -= len(missing) * 8.0
    confidence = _clamp(confidence, 0.0, 75.0)

    explanation = [
        f"ожидаемое суммарное число карточек за матч: {round(combined, 1)} (линия {line})",
        "оценка основана только на средних показателях, без данных по отдельным матчам",
    ]
    return _finalize(f"Карточки больше {line}", "additional_stats", "cards_over", confidence, False, missing,
                      explanation, ["средние показатели карточек"])


def _shots_market(ctx: MatchContext, name: str, fam: str, on_target: bool) -> MarketResult:
    missing = []
    if not ctx.home_shots.available:
        missing.append(f"нет данных об ударах {ctx.home_team}: {ctx.home_shots.reason}")
    if not ctx.away_shots.available:
        missing.append(f"нет данных об ударах {ctx.away_team}: {ctx.away_shots.reason}")
    if not ctx.home_shots.available and not ctx.away_shots.available:
        return _build_unavailable(name, "additional_stats", fam, missing)

    combined = 0.0
    samples = []
    if ctx.home_shots.available:
        combined += ctx.home_shots.value.avg_on_target if on_target else ctx.home_shots.value.avg_total
        samples.append(ctx.home_shots.value.matches_counted)
    if ctx.away_shots.available:
        combined += ctx.away_shots.value.avg_on_target if on_target else ctx.away_shots.value.avg_total
        samples.append(ctx.away_shots.value.matches_counted)
    line = _nearest_half_line(combined)
    sample_total = min(samples) if samples else 0

    base_confidence = 60.0 if combined > line else 40.0
    confidence = base_confidence * (0.5 + 0.5 * _sample_factor(sample_total))
    confidence -= len(missing) * 8.0
    confidence = _clamp(confidence, 0.0, 75.0)

    label = "ударов в створ" if on_target else "ударов"
    explanation = [
        f"ожидаемое суммарное число {label} за матч: {round(combined, 1)} (линия {line})",
        "оценка основана только на средних показателях, без данных по отдельным матчам",
    ]
    return _finalize(f"{name} {line}", "additional_stats", fam, confidence, False, missing, explanation,
                      [f"средние показатели: {label}"])


# ---------------------------------------------------------------------------
# Market group: correct score
# ---------------------------------------------------------------------------

def _expected_goals(ctx: MatchContext) -> Tuple[Optional[float], Optional[float], List[str]]:
    home_matches = _team_matches(ctx.home_last)
    away_matches = _team_matches(ctx.away_last)
    missing = []

    def avg_scored(matches, team):
        vals = [r[0] for m in matches if (r := _team_perspective(m, team)) is not None]
        return sum(vals) / len(vals) if vals else None

    def avg_conceded(matches, team):
        vals = [r[1] for m in matches if (r := _team_perspective(m, team)) is not None]
        return sum(vals) / len(vals) if vals else None

    home_scored = avg_scored(home_matches, ctx.home_team)
    away_conceded = avg_conceded(away_matches, ctx.away_team)
    away_scored = avg_scored(away_matches, ctx.away_team)
    home_conceded = avg_conceded(home_matches, ctx.home_team)

    if home_scored is None or away_conceded is None:
        missing.append(f"недостаточно данных о голах {ctx.home_team}")
        expected_home = None
    else:
        expected_home = (home_scored + away_conceded) / 2.0

    if away_scored is None or home_conceded is None:
        missing.append(f"недостаточно данных о голах {ctx.away_team}")
        expected_away = None
    else:
        expected_away = (away_scored + home_conceded) / 2.0

    return expected_home, expected_away, missing


def _correct_score_markets(ctx: MatchContext, broad_market_confidences: List[float]) -> List[MarketResult]:
    expected_home, expected_away, missing = _expected_goals(ctx)

    if expected_home is None or expected_away is None:
        return [_build_unavailable("Точный счёт", "correct_score", "correct_score", missing)]

    # Deterministic candidate search over a small realistic grid -- no randomness.
    candidates = []
    for h in range(0, 5):
        for a in range(0, 5):
            distance = abs(h - expected_home) + abs(a - expected_away)
            weight = 1.0 / (1.0 + distance)
            candidates.append((weight, h, a))
    candidates.sort(key=lambda c: c[0], reverse=True)
    top3 = candidates[:3]

    max_broad = max(broad_market_confidences) if broad_market_confidences else 60.0
    results = []
    for weight, h, a in top3:
        # Always kept well below broader-market confidence -- never a "safe" pick.
        raw = weight * 45.0
        capped = min(raw, max_broad - 10.0, 40.0)
        confidence = _clamp(capped, 3.0, 40.0)
        explanation = [
            f"ожидаемые голы: {ctx.home_team} ≈ {round(expected_home, 2)}, {ctx.away_team} ≈ {round(expected_away, 2)}",
            "точный счёт всегда менее предсказуем, чем общие рынки, и не должен считаться надёжной ставкой",
        ]
        results.append(_finalize(f"Точный счёт {h}:{a}", "correct_score", "correct_score", confidence, False,
                                  missing, explanation, ["ожидаемые голы команд"]))
    return results


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def analyze_match(
    provider: FootballStatisticsProvider,
    home_team: str,
    away_team: str,
    league: Optional[str] = None,
    count: int = 10,
) -> Tuple[MatchContext, List[MarketResult]]:
    """
    Builds the full set of MarketResult objects for one match using only
    data available through `provider`. Returns (context, market_results).
    """
    ctx = gather_match_context(provider, home_team, away_team, league, count)

    results: List[MarketResult] = []
    results.extend(_match_result_markets(ctx))
    results.extend(_goals_markets(ctx))
    results.extend(_half_markets(ctx))
    results.extend(_additional_stats_markets(ctx))

    broad_confidences = [r.confidence for r in results if r.status != STATUS_UNAVAILABLE]
    results.extend(_correct_score_markets(ctx, broad_confidences))

    return ctx, results
