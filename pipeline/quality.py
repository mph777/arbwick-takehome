"""Stage 0 - the data-quality gate.

Runs on the already-truncated frame, so it asks "is the history available at t
good enough to reason about?" and never "is the file good?". Integrity problems
live here; how much history a particular statistic needs lives in that
statistic's stage.

Gaps are evaluated over the trailing window the stages actually consume, not
over all history: a hole in early 2023 must not permanently mute a symbol in
2026, but a hole inside the ES lookback must.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

import config as cfg
from pipeline.models import DataQualityReport

STAGE = "data_quality"
# The deepest lookback any downstream stage uses.
TRAILING_WINDOW = cfg.MIN_CANDLES_ES


def _gap_runs(dates: list[date]) -> list[tuple[date, date, int]]:
    """Runs of consecutive missing calendar days inside `dates`."""
    if len(dates) < 2:
        return []
    present = set(dates)
    runs: list[tuple[date, date, int]] = []
    run_start: date | None = None
    prev_missing: date | None = None
    day = dates[0] + timedelta(days=1)
    while day < dates[-1]:
        if day not in present:
            if run_start is None:
                run_start = day
            prev_missing = day
        elif run_start is not None:
            runs.append((run_start, prev_missing, (prev_missing - run_start).days + 1))
            run_start = prev_missing = None
        day += timedelta(days=1)
    if run_start is not None:
        runs.append((run_start, prev_missing, (prev_missing - run_start).days + 1))
    return runs


def check(symbol: str, df: pd.DataFrame, as_of: date) -> DataQualityReport:
    issues: list[str] = []
    codes: list[str] = []

    def flag(code: str, text: str) -> None:
        codes.append(code)
        issues.append(text)

    if df.empty:
        return DataQualityReport(
            symbol=symbol, as_of=as_of, n_candles=0, first_date=None, last_date=None,
            max_consecutive_gap_days=0,
            reason_codes=("no_data",),
            issues=("no candles available at or before the as-of date",),
        )

    if df["open_time"].duplicated().any():
        n = int(df["open_time"].duplicated().sum())
        flag("duplicate_timestamps", f"{n} duplicate open_time row(s)")
    if not df["open_time"].is_monotonic_increasing:
        flag("unordered_timestamps", "open_time is not strictly increasing")

    tail = df.tail(TRAILING_WINDOW)
    ohlcv = ["open", "high", "low", "close", "volume"]
    if tail[ohlcv].isna().any().any():
        cols = [c for c in ohlcv if tail[c].isna().any()]
        flag("missing_values", f"NaN in {','.join(cols)} within the trailing window")
    if (tail["close"] <= 0).any():
        flag("unusable_prices", "non-positive close within the trailing window")
    if (tail["high"] < tail["low"]).any():
        flag("inconsistent_candles", "high < low within the trailing window")
    if (tail["volume"] < 0).any():
        flag("negative_volume", "negative volume within the trailing window")

    gaps = _gap_runs(list(tail["date"]))
    max_gap = max((g[2] for g in gaps), default=0)
    if max_gap > cfg.MAX_CONSECUTIVE_GAP_DAYS:
        worst = max(gaps, key=lambda g: g[2])
        flag(
            "gap_in_window",
            f"{worst[2]} consecutive missing days ({worst[0]} to {worst[1]}) "
            f"inside the trailing {TRAILING_WINDOW}-candle window; limit is "
            f"{cfg.MAX_CONSECUTIVE_GAP_DAYS}"
        )

    last_date = df["date"].iloc[-1]
    staleness = (as_of - last_date).days
    if staleness > cfg.MAX_STALENESS_DAYS:
        flag(
            "stale_feed",
            f"feed is stale: last candle {last_date} is {staleness} days before "
            f"the as-of date (limit {cfg.MAX_STALENESS_DAYS})"
        )

    zero_tail = 0
    for v in reversed(list(df["volume"].tail(cfg.MAX_ZERO_VOLUME_TAIL))):
        if v == 0:
            zero_tail += 1
        else:
            break
    if zero_tail >= cfg.MAX_ZERO_VOLUME_TAIL:
        flag("zero_volume_tail",
             f"{zero_tail} consecutive zero-volume days ending at {last_date}")

    return DataQualityReport(
        symbol=symbol,
        as_of=as_of,
        n_candles=len(df),
        first_date=df["date"].iloc[0],
        last_date=last_date,
        max_consecutive_gap_days=max_gap,
        reason_codes=tuple(codes),
        issues=tuple(issues),
    )
