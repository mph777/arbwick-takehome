"""Point-in-time correctness.

Two properties, and the second is the one that actually catches bugs:

  A) Truncation equivalence - running as-of t against the full snapshot must
     equal running as-of t against a snapshot physically truncated at t.
  B) Future invariance - appending arbitrary candles AFTER t must not change the
     as-of-t output by so much as a bit.

(B) is what fails when a rolling window is computed before the cut, when a
percentile ranks against the whole file, or when `.max()` is taken on an
unfiltered frame. It is cheap to run and impossible to satisfy accidentally.
"""

from __future__ import annotations

import csv
from datetime import timedelta

import pytest

import config as cfg
from pipeline import quality, regime as regime_stage, risk as risk_stage
from pipeline.loader import as_of_cutoff_ms, clear_cache, load_as_of
from tests.conftest import make_rows, write_snapshot


def _reads(snapshot, symbol, as_of):
    df = load_as_of(symbol, as_of_cutoff_ms(as_of), snapshot)
    reg = regime_stage.classify(symbol, df, as_of)
    rsk = risk_stage.assess(symbol, df, reg)
    return reg, rsk


def test_cutoff_includes_day_t_and_excludes_day_after(clean_symbol):
    snapshot, symbol, as_of = clean_symbol
    df = load_as_of(symbol, as_of_cutoff_ms(as_of), snapshot)
    assert df["date"].iloc[-1] == as_of, "the candle of day t must be included"

    earlier = load_as_of(symbol, as_of_cutoff_ms(as_of - timedelta(days=1)), snapshot)
    assert earlier["date"].iloc[-1] == as_of - timedelta(days=1)
    assert len(earlier) == len(df) - 1


def test_truncated_snapshot_reproduces_full_snapshot(clean_symbol, tmp_path):
    snapshot, symbol, as_of = clean_symbol
    full_reg, full_risk = _reads(snapshot, symbol, as_of)

    rows = list(csv.reader((snapshot / f"{symbol}.csv").open()))[1:]
    kept = [r for r in rows if int(r[6]) < as_of_cutoff_ms(as_of)]
    truncated = write_snapshot(tmp_path / "trunc", {symbol: kept}, "LATEUSDT")

    t_reg, t_risk = _reads(truncated, symbol, as_of)
    assert t_reg.model_dump() == full_reg.model_dump()
    assert t_risk.model_dump() == full_risk.model_dump()


@pytest.mark.parametrize("shock", [10.0, 0.1])
def test_future_candles_cannot_move_an_earlier_decision(clean_symbol, tmp_path, shock):
    """Bolt a violent move onto the end of the file. Nothing at t may change."""
    snapshot, symbol, as_of = clean_symbol
    before_reg, before_risk = _reads(snapshot, symbol, as_of)

    rows = list(csv.reader((snapshot / f"{symbol}.csv").open()))[1:]
    keep = [r for r in rows if int(r[6]) < as_of_cutoff_ms(as_of)]
    last_close = float(keep[-1][4])
    future = make_rows(120, seed=99, start=as_of + timedelta(days=1),
                       drift=0.0, vol=0.20, price0=last_close * shock)
    contaminated = write_snapshot(tmp_path / "future", {symbol: keep + future}, "LATEUSDT")

    after_reg, after_risk = _reads(contaminated, symbol, as_of)
    assert after_reg.model_dump() == before_reg.model_dump()
    assert after_risk.model_dump() == before_risk.model_dump()


def test_quality_gate_also_ignores_the_future(clean_symbol, tmp_path):
    """A hole AFTER t must not refuse a date that was fine at the time."""
    snapshot, symbol, as_of = clean_symbol
    rows = list(csv.reader((snapshot / f"{symbol}.csv").open()))[1:]
    keep = [r for r in rows if int(r[6]) < as_of_cutoff_ms(as_of)]
    future = make_rows(60, seed=7, start=as_of + timedelta(days=30))  # 29-day hole
    contaminated = write_snapshot(tmp_path / "hole", {symbol: keep + future}, "LATEUSDT")

    df = load_as_of(symbol, as_of_cutoff_ms(as_of), contaminated)
    assert quality.check(symbol, df, as_of).ok


def test_rerunning_an_earlier_date_is_stable(clean_symbol):
    snapshot, symbol, _ = clean_symbol
    as_of = cfg.WINDOW_START + timedelta(days=400)
    first = _reads(snapshot, symbol, as_of)
    clear_cache()
    second = _reads(snapshot, symbol, as_of)
    assert [x.model_dump() for x in first] == [x.model_dump() for x in second]
