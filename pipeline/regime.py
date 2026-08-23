"""Stage 1 - regime classification. Deterministic, point-in-time by construction.

Method: a 2x2 taxonomy from two rolling statistics.

  trend  = close_t / close_{t-20} - 1        ->  bull if >= 0 else bear
  vol    = stdev of 20 daily log returns, annualised
  vol_pctl = rank of today's vol within every 20d vol observed up to t
                                             ->  high_vol if >= median else low

Why rolling statistics rather than a fitted model (HMM, clustering): a fitted
regime model has to be estimated on a sample, and the honest version of that
re-estimates at every as-of date on data <= t only. That is both slow and, far
worse, easy to get subtly wrong - one `fit()` on the full frame and the whole
decision log is contaminated in a way that still looks plausible. A rolling
statistic is point-in-time by construction: the only way to leak is to hand the
function data it should not have, which is prevented once, in the loader.

Known property, not a bug: the trend leg flips whenever the 20d return crosses
zero, so the regime is noisy in flat markets. Adding hysteresis would mean
carrying state between dates, and state read from the previous decision would
break "re-running as-of an earlier date reproduces that entry". If smoothing
were wanted it would have to be another pure function of data <= t. The
flip rate is reported in the writeup rather than hidden.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

import config as cfg
from pipeline.models import RegimeOutput, SkillRefusal

STAGE = "regime"


def _log_returns(close: pd.Series) -> pd.Series:
    return np.log(close / close.shift(1)).dropna()


def realized_vol_series(close: pd.Series, window: int) -> pd.Series:
    """Annualised realised volatility of daily log returns, rolling `window`."""
    rets = _log_returns(close)
    return rets.rolling(window).std(ddof=1).dropna() * math.sqrt(cfg.ANNUALISATION_DAYS)


def percentile_of_last(series: pd.Series) -> float:
    """Where today's value sits in its own history up to t, inclusive.

    Inclusive of the current observation on purpose: the alternative ranks today
    against a sample it is not part of, which makes the first observation of a
    new volatility regime score 1.0 by definition.
    """
    return float((series <= series.iloc[-1]).mean())


def classify(symbol: str, df: pd.DataFrame, as_of: date) -> RegimeOutput:
    n = len(df)
    if n < cfg.MIN_CANDLES_REGIME:
        raise SkillRefusal(
            STAGE, "insufficient_history",
            f"{n} daily candles at {as_of}, need {cfg.MIN_CANDLES_REGIME}",
        )

    close = df["close"].astype(float)
    if close.isna().any() or (close <= 0).any():
        raise SkillRefusal(STAGE, "unusable_prices",
                           "non-positive or missing close within the available history")

    vol = realized_vol_series(close, cfg.VOL_WINDOW_REGIME)
    if len(vol) < cfg.MIN_VOL_OBSERVATIONS:
        raise SkillRefusal(
            STAGE, "insufficient_vol_sample",
            f"{len(vol)} rolling {cfg.VOL_WINDOW_REGIME}d volatility observations, "
            f"need {cfg.MIN_VOL_OBSERVATIONS} before a percentile means anything",
        )

    current_vol = float(vol.iloc[-1])
    if not math.isfinite(current_vol):
        raise SkillRefusal(STAGE, "degenerate_volatility",
                           "realised volatility is not finite")
    if current_vol == 0.0:
        raise SkillRefusal(
            STAGE, "degenerate_volatility",
            f"realised volatility is exactly zero over {cfg.VOL_WINDOW_REGIME} days - "
            f"the feed is flat, not calm",
        )

    trend = float(close.iloc[-1] / close.iloc[-1 - cfg.TREND_WINDOW] - 1.0)
    if not math.isfinite(trend):
        raise SkillRefusal(STAGE, "degenerate_trend", "trend return is not finite")

    vol_pctl = percentile_of_last(vol)
    direction = "bull" if trend >= 0 else "bear"
    vol_state = "high_vol" if vol_pctl >= cfg.VOL_PCTL_SPLIT else "low_vol"

    return RegimeOutput(
        symbol=symbol,
        as_of=as_of,
        regime=f"{direction}_{vol_state}",
        trend_return=trend,
        trend_window=cfg.TREND_WINDOW,
        realized_vol=current_vol,
        vol_percentile=vol_pctl,
        vol_window=cfg.VOL_WINDOW_REGIME,
        n_candles_used=n,
        n_vol_observations=len(vol),
        method_version=cfg.REGIME_METHOD_VERSION,
    )
