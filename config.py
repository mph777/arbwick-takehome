"""Single source of truth for every constant the pipeline depends on.

Anything that changes a decision lives here, so a reviewer can see the whole
parameterisation on one screen and the prompt/parameter hashes in the decision
log are meaningful.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SNAPSHOT_DIR = REPO_ROOT / "data" / "snapshot"
LOG_DIR = REPO_ROOT / "logs"
LLM_CACHE_DIR = REPO_ROOT / "llm_cache"

# --------------------------------------------------------------------------
# Data window (hard bounds from the brief - do not widen)
# --------------------------------------------------------------------------
WINDOW_START = date(2023, 1, 1)
WINDOW_END = date(2026, 7, 31)

CORE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

# The fourth symbol must have started trading after this date. It is chosen
# empirically by data/fetch.py (see LATE_SYMBOL_SELECTION_FILE) rather than
# hard-coded from memory.
LATE_LISTING_AFTER = date(2025, 6, 1)

# Among the symbols satisfying that constraint, discovery requires one that has
# accumulated MIN_CANDLES_ES candles by the window end. A symbol listed weeks
# before the window closes technically satisfies the brief but can only ever
# produce refusals; requiring it to reach the history gate means the decision log
# shows the gate OPENING - refusals with a reason, then decisions - which is the
# behaviour worth demonstrating.
#
# Candidates are ranked by median daily quote volume over a FIXED trailing slice
# of the window, not over each symbol's own history: medians taken over windows
# of different lengths are not comparable, and ranking on them systematically
# favours whichever symbol listed most recently - precisely the symbol least able
# to show anything.
LATE_RANK_TRAILING_DAYS = 30

# Once chosen, the symbol is pinned here so a later re-fetch cannot silently swap
# it (e.g. because a higher-ranked candidate was delisted meanwhile).
# None = let discovery choose.
LATE_SYMBOL_PIN: str | None = None

LATE_SYMBOL_SELECTION_FILE = SNAPSHOT_DIR / "late_symbol_selection.json"
MANIFEST_FILE = SNAPSHOT_DIR / "manifest.json"

BINANCE_BASE_URL = "https://api.binance.com"
KLINES_LIMIT = 1000  # exchange maximum per request
KLINE_INTERVAL = "1d"
DAY_MS = 86_400_000

# --------------------------------------------------------------------------
# Point-in-time semantics
# --------------------------------------------------------------------------
# A decision dated t is stamped 00:00:00.000 UTC of t+1 and may use every daily
# candle that has CLOSED by then - i.e. the candle of day t is included, and
# nothing later exists. The cutoff is computed once in the orchestrator and
# applied once in pipeline/loader.py; no stage ever sees unfiltered data.
#
# A snapshot whose last candle is older than this many days relative to t means
# the feed is stale and the pipeline refuses rather than extrapolating.
MAX_STALENESS_DAYS = 3

# --------------------------------------------------------------------------
# Stage 1 - regime
# --------------------------------------------------------------------------
REGIME_METHOD_VERSION = "regime/rolling-2x2/v1"
TREND_WINDOW = 20          # trading days for the trend leg
VOL_WINDOW_REGIME = 20     # trading days for the realised-vol leg
VOL_PCTL_SPLIT = 0.50      # >= this percentile of own history -> high_vol
MIN_CANDLES_REGIME = 60    # hard floor on history before Stage 1 will speak
MIN_VOL_OBSERVATIONS = 40  # observations needed before a percentile is meaningful
ANNUALISATION_DAYS = 365   # crypto trades every calendar day

# --------------------------------------------------------------------------
# Stage 2 - risk
# --------------------------------------------------------------------------
RISK_METHOD_VERSION = "risk/dd-volpctl-es95/v1"
VOL_WINDOW_RISK = 30
MIN_CANDLES_RISK = 90          # 30d vol needs >= 60 observations to rank against

# One calendar year of daily returns - 365, not the TradFi 252. Binance spot
# trades every calendar day, so 252 would be a convention imported from a market
# that closes at weekends, and it would contradict the sqrt(365) annualisation
# used for volatility two blocks above. Two different notions of "a year" inside
# one risk module is the kind of thing that is never wrong enough to break a
# test and always wrong enough to be embarrassing in review.
ES_WINDOW = 365
ES_CONFIDENCE = 0.95
MIN_CANDLES_ES = ES_WINDOW + 1  # 366 closes -> 365 returns, no short-window fallback

# --------------------------------------------------------------------------
# Data-quality gate (evaluated on data <= cutoff only)
# --------------------------------------------------------------------------
MAX_CONSECUTIVE_GAP_DAYS = 5   # more consecutive missing calendar days -> refuse
MAX_ZERO_VOLUME_TAIL = 5       # trailing days of zero volume -> refuse

# --------------------------------------------------------------------------
# Stage 3 - allocation agent
# --------------------------------------------------------------------------
LLM_MODEL = "claude-3-5-haiku-20241022"
LLM_MAX_TOKENS = 2048
LLM_TEMPERATURE = 0.0
LLM_MAX_RETRIES = 1  # one reprompt on schema failure, then refuse

STANCES = ("risk_on", "neutral", "risk_off")
TILT_QUANTUM = 0.05

# Tilt ceilings by final stance (applied after any stance override).
TILT_CEILING_BY_STANCE = {"risk_on": 1.00, "neutral": 0.60, "risk_off": 0.30}

# Constraint thresholds
C_EXTREME_VOL_PCTL = 0.90   # bear_high_vol above this -> risk_on forbidden
C_DEEP_DRAWDOWN = -0.35     # drawdown at or below this -> risk_on forbidden, tilt capped
C_DEEP_DRAWDOWN_TILT_CAP = 0.50
C_SEVERE_ES = -0.08         # 1d ES95 at or below this -> tilt capped
C_SEVERE_ES_TILT_CAP = 0.40

# --------------------------------------------------------------------------
# Decision log
# --------------------------------------------------------------------------
LOG_SCHEMA_VERSION = "decision-log/v1"
DECISION_LOG_FILE = LOG_DIR / "decisions.jsonl"
