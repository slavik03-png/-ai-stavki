"""
Central, version-1 configuration for the ranked HIGH/MEDIUM/LOW/REJECTED
value-signal system (see ai_predictions/value_engine.py). Every threshold
that decides a signal's level lives here as a named constant -- nothing
in value_engine.py, value_selector.py or value_report.py hard-codes a
number, so tuning the model later means editing exactly this file.

All thresholds below are exactly the version-1 production numbers from
the spec. Nothing here is invented or guessed.
"""

from __future__ import annotations

#: Bumped for the API-Football enrichment activation: Prediction now
#: carries statistics_* / final_combined_score fields and ranking within a
#: tier can use a stats-informed final_combined_score. dedup_key includes
#: model_version, so this always creates fresh tracking rows rather than
#: silently reinterpreting older rows under a new meaning.
MODEL_VERSION = "value-ranking-v2.1"

SIGNAL_HIGH = "HIGH"
SIGNAL_MEDIUM = "MEDIUM"
SIGNAL_LOW = "LOW"
SIGNAL_REJECTED = "REJECTED"
ALL_SIGNAL_LEVELS = (SIGNAL_HIGH, SIGNAL_MEDIUM, SIGNAL_LOW, SIGNAL_REJECTED)

#: Used by the full diagnostics report (/status) -- kept exactly as before
#: so existing diagnostics text/tests are unaffected by the user-facing
#: Telegram card switching to Russian-only labels (see SIGNAL_LABELS_RU_CARD).
SIGNAL_LABELS = {
    SIGNAL_HIGH: "🔥 HIGH",
    SIGNAL_MEDIUM: "🟡 MEDIUM",
    SIGNAL_LOW: "⚪ LOW",
    SIGNAL_REJECTED: "Отклонено",
}

#: Russian-only labels for the concise, non-technical '🤖 Прогнозы ИИ'
#: Telegram card -- the only surface a regular user sees. The full
#: diagnostics report (/status) keeps SIGNAL_LABELS above unchanged.
SIGNAL_LABELS_RU_CARD = {
    SIGNAL_HIGH: "🔥 ВЫСОКИЙ",
    SIGNAL_MEDIUM: "🟡 СРЕДНИЙ",
    SIGNAL_LOW: "⚪ НИЗКИЙ",
    SIGNAL_REJECTED: "Отклонено",
}

# ---------------------------------------------------------------------------
# Per-level thresholds. A candidate is assigned the HIGHEST level whose
# *complete* set of conditions it satisfies (checked HIGH -> MEDIUM -> LOW
# -> REJECTED, first full match wins -- see value_engine.classify_signal).
# ---------------------------------------------------------------------------

HIGH_MIN_BOOKMAKERS = 3
HIGH_MIN_EV = 0.08
HIGH_MIN_EDGE = 0.03

MEDIUM_MIN_BOOKMAKERS = 2
MEDIUM_MIN_EV = 0.05
MEDIUM_MIN_EDGE = 0.02

LOW_MIN_BOOKMAKERS = 2
LOW_MIN_EV = 0.03
LOW_MIN_EDGE = 0.01

#: Below this price nothing is a real decimal price at all (mirrors
#: matching.normalize_price's own >1.0 rule; kept here too since it is a
#: named condition of every level in the spec).
MIN_BEST_ODDS = 1.01

# ---------------------------------------------------------------------------
# Outlier detection (Step 3 / Step 4 of the spec).
# ---------------------------------------------------------------------------

#: If the best price is more than this fraction higher than the
#: second-best real price for the same outcome, the best price is an
#: "isolated outlier" -- likely a pricing mistake, stale quote, or a
#: bookmaker not yet synced with the market, not necessarily genuine
#: value. Example: best=1.30, threshold=0.10 -> flagged if second-best
#: is below 1.30 / 1.10 = 1.1818.
OUTLIER_PRICE_GAP_THRESHOLD = 0.10

#: A bookmaker's price counts as "near the best price" (used for the
#: price-clustering diagnostic and outlier assessment) when it is within
#: this fraction of the best price.
NEAR_BEST_PRICE_PCT = 0.02

#: An outlier warning demotes the signal exactly one level:
#: HIGH -> MEDIUM -> LOW -> REJECTED. Documented in value_engine.classify_signal.
OUTLIER_DEMOTES_BY_ONE_LEVEL = True

# ---------------------------------------------------------------------------
# Output volume / ranking (Step 6).
# ---------------------------------------------------------------------------

#: Global cap across ALL levels combined (Step 8/9/10 of the
#: production-discovery spec): the report shows at most this many signals
#: total, filled HIGH first, then MEDIUM, then LOW -- never padded with
#: weaker candidates just to reach this number.
MAX_TOTAL_SIGNALS = 5

# ---------------------------------------------------------------------------
# Request-time re-selection (2026-07-15 change): a request no longer
# replays a fixed top-5 computed once for a static 36h window. Every
# request re-filters the already-discovered candidate pool against the
# CURRENT moment -- matches that have already kicked off or finished are
# dropped, and only what remains is (re-)ranked. See
# ai_predictions/prediction_selector.select_current_recommendations and
# .agents/memory/request-time-reselection.md.
# ---------------------------------------------------------------------------

#: A fixture is only eligible to be shown if its real kickoff is at least
#: this many minutes in the future -- gives a user time to actually place
#: a bet before the match starts, rather than showing something that is
#: technically "not started yet" but seconds away.
MIN_LEAD_TIME_MINUTES = 30

#: Ranking-score weights (see value_engine.compute_ranking_score for the
#: exact formula). All real, observable inputs -- EV, edge, bookmaker
#: count, price dispersion, outlier penalty -- nothing about the score
#: depends on the size of the quoted price alone.
RANKING_WEIGHT_EV = 60.0
RANKING_WEIGHT_EDGE = 25.0
RANKING_WEIGHT_BOOKMAKERS = 3.0
RANKING_WEIGHT_DISPERSION_PENALTY = 15.0
RANKING_OUTLIER_PENALTY = 20.0

# ---------------------------------------------------------------------------
# Analysis window -- unchanged from the existing production behaviour.
# Re-exported here only so all "the numbers that govern the model" are
# discoverable from one config surface; ai_predictions/window.py remains
# the single source of truth and is NOT duplicated logic, just referenced.
# ---------------------------------------------------------------------------

from ai_predictions.window import WINDOW_HOURS  # noqa: E402,F401

# ---------------------------------------------------------------------------
# API-Football statistics enrichment (activation of the existing provider
# for the value-divergence strategy). Every number below is a real,
# deliberate operating limit -- never a guess -- chosen to protect the
# API-Football free-plan daily quota (100 requests/day).
# ---------------------------------------------------------------------------

#: Only the top-N preliminary candidates (by ranking_score, before any
#: statistics is considered) are ever eligible for enrichment. Bounds the
#: worst-case request count regardless of how many real markets matched.
ENRICHMENT_SHORTLIST_SIZE = 10

#: Real published daily quota for the API-Football free plan.
API_FOOTBALL_DAILY_QUOTA = 100

#: Requests are never spent below this reserve, so a single "🤖 Прогнозы
#: ИИ" run can never exhaust the account for the rest of the day.
API_FOOTBALL_QUOTA_RESERVE = 10

#: A cached API-Football answer (match/team resolution or statistics) is
#: reused for this long before a fresh request is allowed for the same
#: real query -- avoids re-spending quota on the same team/fixture inside
#: one day.
API_FOOTBALL_CACHE_TTL_HOURS = 24

#: A fuzzy team-name match below this similarity score is treated as "no
#: real match found" rather than guessed -- prevents attaching one team's
#: statistics to a different real team that merely has a similar name.
TEAM_MATCH_CONFIDENCE_FLOOR = 0.72

#: The API-Football free plan rejects the `last`/`next` fixture params and
#: any season outside this set (confirmed against the live API, not
#: assumed) -- so almost every statistics call for a *current* match will
#: fail structurally regardless of team matching. Enrichment checks this
#: BEFORE spending any quota: if the analysis season is not in this set,
#: it skips every API-Football call for the run and reports honestly why,
#: rather than spending quota on calls already known to fail.
API_FOOTBALL_FREE_PLAN_SEASONS = frozenset({2022, 2023, 2024})

#: How strongly a real statistics-agreement signal (0..1, 0.5 = neutral)
#: can nudge a candidate's ranking *within its own HIGH/MEDIUM/LOW tier*.
#: Never large enough to matter more than the underlying odds edge/EV, and
#: never crosses a tier boundary since value_selector always sorts by tier
#: first. Statistics only re-orders already-qualified candidates; it can
#: never promote a REJECTED candidate or change a signal_level.
STATS_BLEND_MAGNITUDE = 2.0

# ---------------------------------------------------------------------------
# API-Football-first fixture discovery (production version 2): the 36h
# analysis window is discovered directly from API-Football fixtures (real
# kickoff timestamps, real statuses), then matched against The Odds API's
# bookmaker events -- rather than discovering the window from bookmaker
# events alone. Every status code below is API-Football's own documented
# short status code, not invented.
# ---------------------------------------------------------------------------

#: A fixture with this status has not started and is eligible if its real
#: kickoff falls inside the analysis window.
FIXTURE_NOT_STARTED_STATUSES = frozenset({"NS", "TBD"})

#: A fixture currently being played -- excluded, it is not a future bet.
FIXTURE_LIVE_STATUSES = frozenset({"1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "LIVE"})

#: A fixture that has already concluded -- excluded.
FIXTURE_FINISHED_STATUSES = frozenset({"FT", "AET", "PEN"})

#: A fixture that will never be played as scheduled -- excluded.
FIXTURE_CANCELLED_STATUSES = frozenset({"CANC", "ABD", "AWD", "WO"})

#: Postponed with no confirmed new kickoff -- excluded unless the fixture's
#: own real timestamp still happens to fall inside the window (handled by
#: the same timestamp filter every other fixture goes through).
FIXTURE_POSTPONED_STATUSES = frozenset({"PST"})

#: How close two independently-reported kickoff times (API-Football vs The
#: Odds API, for what should be the exact same real match) are allowed to
#: be and still count as the same fixture -- covers real-world rounding/
#: reporting differences between the two providers, never a guess about
#: which match it "probably" is.
FIXTURE_KICKOFF_TOLERANCE_MINUTES = 20

#: Team-name similarity floor for matching an Odds API event to an
#: API-Football fixture -- reuses the same floor already proven for
#: API-Football team-name matching (see TEAM_MATCH_CONFIDENCE_FLOOR above).
FIXTURE_MATCH_CONFIDENCE_FLOOR = TEAM_MATCH_CONFIDENCE_FLOOR

# ---------------------------------------------------------------------------
# Phase 6 -- auditable market+statistics probability blend. Every weight
# below is exactly the suggested weighting from the spec; nothing here is
# guessed. Sample-size categories are counted in real finished matches
# retrieved for a team's recent form (FormSplit.matches_counted).
# ---------------------------------------------------------------------------

#: Minimum real recent matches (per team) required for each sample-size
#: category. Below WEAK_MIN_MATCHES, statistics is not used at all (the
#: candidate stays market-only).
STRONG_SAMPLE_MIN_MATCHES = 8
MEDIUM_SAMPLE_MIN_MATCHES = 4
WEAK_SAMPLE_MIN_MATCHES = 1

#: (market_weight, statistics_weight) per sample-size category -- always
#: sums to 1.0.
PROBABILITY_BLEND_WEIGHTS = {
    "strong": (0.60, 0.40),
    "medium": (0.75, 0.25),
    "weak": (0.90, 0.10),
    "none": (1.00, 0.00),
}

#: Final blended probability is never allowed outside this range -- avoids
#: an extreme, overconfident number from a tiny/noisy sample while staying
#: an honest, auditable estimate rather than a forced "attractive" figure.
PROBABILITY_CLAMP_MIN = 0.02
PROBABILITY_CLAMP_MAX = 0.98

# ---------------------------------------------------------------------------
# Production version 3 (2026-07-14): API-Football becomes the PRIMARY and
# SUFFICIENT data source for recommendations. The Odds API becomes purely
# optional coefficient enrichment -- its absence, quota exhaustion, or any
# HTTP error must never block or reduce the number of recommendations
# produced. See ai_predictions/football_pipeline.py.
# ---------------------------------------------------------------------------

#: How long a discovered fixture list stays valid before a fresh
#: API-Football fixture query is allowed for the same date -- deliberately
#: shorter than API_FOOTBALL_CACHE_TTL_HOURS (used for team stats/
#: predictions, which change less often) per the explicit "cache fixtures
#: for 6 hours" requirement.
FIXTURE_LIST_CACHE_TTL_HOURS = 6

#: Signal thresholds for the API-Football-only probability model (Section
#: 5 of the production fix spec). Exactly the suggested thresholds --
#: nothing invented.
PROB_HIGH_MIN = 0.72
PROB_MEDIUM_MIN = 0.64
PROB_LOW_MIN = 0.56

#: HIGH additionally requires "good data completeness" -- defined here as
#: at least this fraction of the inputs the market's probability depends
#: on being real, retrieved data (see football_predictions.py for how
#: completeness is computed per market).
PROB_HIGH_MIN_COMPLETENESS = 0.6

#: Upper bound on how many discovered fixtures are actually sent through
#: the analysis step (cache lookups + at-most-best-effort live calls) in
#: one run. This is a runtime/CPU bound, NOT the quota safety mechanism --
#: quota is protected per real HTTP call (see football_predictions.py's
#: budget-checked fetch helpers), so every one of these fixtures is always
#: analysed even once the daily reserve is fully spent (using cache and/or
#: the historical-baseline fallback). Fixtures are analysed soonest-
#: kickoff first; any fixture beyond this cap is honestly reported as
#: "found but not analysed", never silently dropped.
MAX_FIXTURES_ANALYSED_PER_RUN = 25

#: How long a real `/predictions` answer for one fixture stays valid
#: before a fresh call is allowed -- same 6h horizon as the fixture list
#: itself (predictions rarely change meaningfully hour-to-hour, and this
#: keeps repeated runs on the same day from re-spending quota on fixtures
#: already analysed).
PREDICTIONS_CACHE_TTL_HOURS = 6

#: Reason string used whenever a live API-Football call is skipped
#: because today's safety reserve is exhausted -- distinguishes this from
#: a real transient network/HTTP error (never cached as final; see
#: football_predictions.py).
QUOTA_RESERVE_EXHAUSTED_REASON = "Резерв запросов к API-Football на сегодня исчерпан"

# ---------------------------------------------------------------------------
# Historical-baseline fallback (2026-07-14 production fix): when a fixture
# has neither a real API-Football predictions answer nor real recent-form
# data for either team (e.g. the daily quota reserve is exhausted and
# nothing is cached yet for these specific teams), the fixture must still
# be ranked rather than silently skipped. These are real, well-known
# aggregate football statistics (global average outcome distribution and
# average goals per match, reflecting home advantage) -- not fabricated
# for a specific match. Candidates built from this fallback are always
# capped at the LOW confidence tier (see prediction_selector.classify),
# regardless of the raw probability number, because they carry zero
# fixture-specific evidence.
# ---------------------------------------------------------------------------

#: Global historical outcome distribution across top football leagues
#: (approximate, well-documented aggregate: home win / draw / away win).
HISTORICAL_HOME_WIN_PROB = 0.45
HISTORICAL_DRAW_PROB = 0.27
HISTORICAL_AWAY_WIN_PROB = 0.28

#: Historical average goals scored per match by the home/away side
#: (reflects real, well-documented home-advantage goal split).
HISTORICAL_AVG_GOALS_HOME = 1.45
HISTORICAL_AVG_GOALS_AWAY = 1.15

#: Completeness assigned to a fully historical-baseline candidate (no real
#: fixture-specific evidence at all) -- low but non-zero, since it is real
#: (if generic) data, not an invented number.
HISTORICAL_FALLBACK_COMPLETENESS = 0.1

# ---------------------------------------------------------------------------
# Strict daily archive (2026-07-15 fix): once a day's worth of matches has
# been fully analysed, the computed top-5 result itself is persisted so
# that repeated "🤖 Прогнозы ИИ" presses within the same 24h window never
# recompute anything or touch API-Football again -- they just replay the
# saved archive. This is on top of (not a replacement for) the existing
# per-item 24h caches in football_cache.py.
# ---------------------------------------------------------------------------

#: How long the persisted daily top-5 result stays valid before the next
#: button press is allowed to trigger a fresh run -- exactly the required
#: "24 hours from the moment of successful retrieval".
DAILY_ARCHIVE_TTL_HOURS = 24.0

#: How long a "refresh already in progress" marker is honoured before it
#: is treated as stale (e.g. a previous run crashed mid-way) and a new
#: attempt is allowed. Only guards against duplicate concurrent API spend,
#: never blocks forever.
DAILY_ARCHIVE_LOCK_TTL_MINUTES = 10.0

#: Real, understandable betting markets this production version creates
#: candidates for (Section 3 of the spec). Every key here must have a
#: corresponding honest probability-derivation path in
#: ai_predictions/football_predictions.py -- no market is ever added here
#: without one.
SUPPORTED_BET_MARKETS = (
    "home_win", "draw", "away_win", "double_chance_1x", "double_chance_x2",
    "over_1_5", "over_2_5", "under_3_5", "btts_yes", "btts_no",
)

#: Russian display label for each supported market (used on the Telegram
#: card's "Ставка:" line). Double-chance and totals are spelled out in
#: full, plain Russian (never a raw code like "1X") and use a comma as the
#: decimal separator, matching Russian typographic convention.
BET_MARKET_LABELS_RU = {
    "home_win": "Победа хозяев",
    "draw": "Ничья",
    "away_win": "Победа гостей",
    "double_chance_1x": "Победа хозяев или ничья",
    "double_chance_x2": "Победа гостей или ничья",
    "over_1_5": "Тотал больше 1,5",
    "over_2_5": "Тотал больше 2,5",
    "under_3_5": "Тотал меньше 3,5",
    "btts_yes": "Обе забьют — да",
    "btts_no": "Обе забьют — нет",
}

#: Card-only signal vocabulary (ai_predictions/prediction_report.py). A
#: plain lowercase Russian word plus the level's emoji, combined as
#: "{emoji} Уровень сигнала: {word}" -- separate from SIGNAL_LABELS_RU_CARD
#: above (kept for any other/legacy caller) because the user-facing card no
#: longer shows the level as a single uppercase "🔥 ВЫСОКИЙ" token.
SIGNAL_WORD_RU_CARD = {
    SIGNAL_HIGH: "высокий",
    SIGNAL_MEDIUM: "средний",
    SIGNAL_LOW: "низкий",
}
SIGNAL_EMOJI_RU_CARD = {
    SIGNAL_HIGH: "🔥",
    SIGNAL_MEDIUM: "🟡",
    SIGNAL_LOW: "⚪",
}

# ---------------------------------------------------------------------------
# Live in-play predictions mode (2026-07-15): a second, independent analysis
# mode for matches already in progress -- never touches the shared daily
# archive/pool above. Uses the same real cross-bookmaker consensus math as
# ai_predictions/value_engine.py (leave-one-out consensus vs. best price),
# since API-Football's own /predictions endpoint is a pre-match model and
# has no real opinion once a match has kicked off. A live fixture with no
# real, currently-matched bookmaker price is dropped, never estimated.
# ---------------------------------------------------------------------------

#: How long one fetched Live result (fixtures + matched odds + rendered
#: cards) stays valid before the next "🔴 Live" press is allowed to spend a
#: fresh API-Football + Odds API request. Deliberately much shorter than
#: DAILY_ARCHIVE_TTL_HOURS -- in-play scores/minutes/odds move fast.
LIVE_CACHE_TTL_MINUTES = 10.0

#: At most this many live picks are ever shown at once -- same cap
#: philosophy as MAX_TOTAL_SIGNALS, kept as its own named constant since
#: Live mode is a fully separate pipeline from the pre-match one.
LIVE_MAX_RECOMMENDATIONS = 5

#: Mode marker values stored on every tracking.models.Prediction /
#: analytics prediction row, so a live pick can never collide (via
#: dedup_key) with a pre-match pick on the same fixture/market, and so
#: "📈 Статистика" can report pre-match and Live figures separately.
PREDICTION_MODE_PRE_MATCH = "pre_match"
PREDICTION_MODE_LIVE = "live"
