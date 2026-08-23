"""Stage 2 - tail-risk read, conditional on the published regime.

Three measures, all computed on data <= t only:

  drawdown_from_ath  close_t / max(close <= t) - 1
  vol_percentile     rank of today's 30d realised vol in its own history <= t
  es_95_1d           mean of the worst 5% of the last 365 daily simple returns

The three sit on deliberately different time scales: the volatility percentile is
the state of the tape now, the drawdown is where the position already is along
its path, and the ES is how bad a bad day looks on a full cycle of evidence. If
all three reacted at the same speed they would be one measure computed three
ways, and the LLM would have nothing to weigh.

Choices worth defending:

* ES is a ONE-DAY measure. The 365 is the sample it is estimated from, not the
  horizon: take the last 365 daily returns, keep the worst 5% (18 observations),
  average them. It answers "on a bad day, what does one day cost", by historical
  simulation - no distributional assumption, which matters in a market whose
  returns are visibly not normal.

* 365, not the TradFi 252. Binance spot trades every calendar day; 252 is the
  session count of a market that closes at weekends. Importing it here would also
  contradict the sqrt(365) annualisation used for volatility, and one module
  holding two different notions of "a year" is a defect even when every number in
  it is individually defensible.

* The window is equally weighted, and that is the real limitation rather than its
  length: a day eleven months old counts as much as yesterday, so in a market
  where volatility clusters hard the ES is slow to acknowledge a regime change.
  Age-weighted or filtered historical simulation (standardise returns by a
  GARCH/EWMA volatility estimate, take the tail of the residuals, rescale by
  today's volatility) is the production answer. It is not used here because it
  introduces fitted parameters, and a fitted parameter has to be re-estimated
  point-in-time at every as-of date or the whole log is contaminated - the same
  argument that keeps Stage 1 on rolling statistics. The inertia is instead
  covered by the 30d volatility percentile sitting next to it.

* Drawdown is measured on closes, not intraday highs. The pipeline's whole
  decision cadence is daily-close, so a drawdown defined against a wick the
  strategy could never have acted on would overstate the state it is in.

* The ES window does not shrink. Below 366 closes there is no ES estimate and
  Stage 2 refuses the whole date for that symbol rather than publishing two of
  three measures or quietly computing a 95% tail from 60 observations - where the
  "worst 5%" is three days, an average that swings on one new observation and
  that will read calm precisely because a short window rarely contains a crash.
  A tail estimate from a sample too small to contain a tail is the exact failure
  this brief is about. The cost is visible and bounded: every symbol refuses for
  its first 365 days of history, which the decision log shows plainly.
"""

from __future__ import annotations

import math
import pandas as pd

import config as cfg
from pipeline.models import RegimeOutput, RiskOutput, SkillRefusal
from pipeline.regime import percentile_of_last, realized_vol_series

STAGE = "risk"


def assess(symbol: str, df: pd.DataFrame, regime: RegimeOutput) -> RiskOutput:
    # Contract check. Stage 2 rejects a malformed upstream payload instead of
    # coercing it: no dicts, no "close enough" symbol or date.
    if not isinstance(regime, RegimeOutput):
        raise TypeError(
            f"Stage 2 requires a RegimeOutput, got {type(regime).__name__}. "
            f"Inter-stage payloads are validated, not coerced."
        )
    if regime.symbol != symbol:
        raise ValueError(f"regime payload is for {regime.symbol}, asked about {symbol}")

    as_of = regime.as_of
    n = len(df)
    if n < cfg.MIN_CANDLES_RISK:
        raise SkillRefusal(
            STAGE, "insufficient_history",
            f"{n} daily candles at {as_of}, need {cfg.MIN_CANDLES_RISK} for a "
            f"{cfg.VOL_WINDOW_RISK}d volatility percentile",
        )
    if n < cfg.MIN_CANDLES_ES:
        raise SkillRefusal(
            STAGE, "insufficient_history_for_es",
            f"{n} daily candles at {as_of}, need {cfg.MIN_CANDLES_ES} for a "
            f"{cfg.ES_WINDOW}-return {int(cfg.ES_CONFIDENCE * 100)}% expected "
            f"shortfall; the window is not shortened",
        )

    close = df["close"].astype(float)

    # --- drawdown ---------------------------------------------------------
    running_max = close.cummax()
    ath_close = float(running_max.iloc[-1])
    ath_idx = int(close.idxmax())
    drawdown = float(close.iloc[-1] / ath_close - 1.0)
    if drawdown > 0:
        # Only reachable if closes are not what they claim to be.
        raise SkillRefusal(STAGE, "inconsistent_drawdown",
                           f"drawdown computed positive ({drawdown:.6f})")

    # --- realised volatility percentile ------------------------------------
    vol = realized_vol_series(close, cfg.VOL_WINDOW_RISK)
    if len(vol) < cfg.VOL_WINDOW_RISK * 2:
        raise SkillRefusal(
            STAGE, "insufficient_vol_sample",
            f"{len(vol)} rolling {cfg.VOL_WINDOW_RISK}d volatility observations, "
            f"need {cfg.VOL_WINDOW_RISK * 2}",
        )
    current_vol = float(vol.iloc[-1])
    if not math.isfinite(current_vol) or current_vol == 0.0:
        raise SkillRefusal(STAGE, "degenerate_volatility",
                           f"{cfg.VOL_WINDOW_RISK}d realised volatility is {current_vol}")
    vol_pctl = percentile_of_last(vol)

    # --- expected shortfall ------------------------------------------------
    rets = close.pct_change().dropna().tail(cfg.ES_WINDOW)
    if len(rets) < cfg.ES_WINDOW:
        raise SkillRefusal(
            STAGE, "insufficient_history_for_es",
            f"{len(rets)} returns available, need {cfg.ES_WINDOW}",
        )
    n_tail = int(len(rets) * (1.0 - cfg.ES_CONFIDENCE))
    if n_tail < 1:
        raise SkillRefusal(STAGE, "insufficient_history_for_es",
                           "tail sample would be empty at this confidence level")
    es = float(rets.sort_values().iloc[:n_tail].mean())
    if not math.isfinite(es):
        raise SkillRefusal(STAGE, "degenerate_es", "expected shortfall is not finite")
    if es > 0:
        raise SkillRefusal(
            STAGE, "implausible_es",
            f"worst {n_tail} of {len(rets)} daily returns average +{es:.4%}; "
            f"a positive tail loss means the return series is wrong",
        )

    return RiskOutput(
        symbol=symbol,
        as_of=as_of,
        regime=regime.regime,
        regime_method_version=regime.method_version,
        drawdown_from_ath=drawdown,
        ath_close=ath_close,
        ath_date=df["date"].iloc[ath_idx],
        realized_vol=current_vol,
        vol_percentile=vol_pctl,
        vol_window=cfg.VOL_WINDOW_RISK,
        es_95_1d=es,
        es_window_days=cfg.ES_WINDOW,
        es_n_tail_observations=n_tail,
        n_candles_used=n,
        method_version=cfg.RISK_METHOD_VERSION,
    )
