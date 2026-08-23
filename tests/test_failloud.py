"""Broken-data behaviour: every defect must surface as an explicit refusal.

The assertion in each case is not merely "it raised" - it is that the reason
code identifies the actual defect. A pipeline that refuses everything with
`unknown_error` is no more honest than one that invents a stance.
"""

from __future__ import annotations

import csv
from datetime import timedelta

import pytest

import config as cfg
from pipeline import quality, regime as regime_stage, risk as risk_stage
from pipeline.loader import as_of_cutoff_ms, load_as_of
from pipeline.models import SkillRefusal
from tests.conftest import START, make_rows, write_snapshot


def rows_of(snapshot, symbol):
    return list(csv.reader((snapshot / f"{symbol}.csv").open()))[1:]


def snap(tmp_path, name, rows):
    return write_snapshot(tmp_path / name, {"BROKENUSDT": rows}, "BROKENUSDT")


def frame(snapshot, as_of, symbol="BROKENUSDT"):
    return load_as_of(symbol, as_of_cutoff_ms(as_of), snapshot)


# --- history floors --------------------------------------------------------


def test_too_little_history_refuses_in_stage_1(tmp_path):
    as_of = START + timedelta(days=40)
    s = snap(tmp_path, "short", make_rows(41, seed=11))
    with pytest.raises(SkillRefusal) as exc:
        regime_stage.classify("BROKENUSDT", frame(s, as_of), as_of)
    assert exc.value.reason_code == "insufficient_history"
    assert exc.value.stage == "regime"


def test_es_window_is_never_shortened(tmp_path):
    """Enough history to classify a regime, not enough for a 252-return tail."""
    as_of = START + timedelta(days=150)
    s = snap(tmp_path, "mid", make_rows(151, seed=12))
    df = frame(s, as_of)
    reg = regime_stage.classify("BROKENUSDT", df, as_of)
    with pytest.raises(SkillRefusal) as exc:
        risk_stage.assess("BROKENUSDT", df, reg)
    assert exc.value.reason_code == "insufficient_history_for_es"


# --- structural defects ----------------------------------------------------


def test_long_gap_refuses(tmp_path):
    head = make_rows(300, seed=13)
    tail = make_rows(60, seed=14, start=START + timedelta(days=320))  # 20-day hole
    as_of = START + timedelta(days=379)
    s = snap(tmp_path, "gap", head + tail)
    report = quality.check("BROKENUSDT", frame(s, as_of), as_of)
    assert not report.ok
    assert any("consecutive missing days" in i for i in report.issues)


def test_duplicate_timestamps_refuse(tmp_path):
    rows = make_rows(300, seed=15)
    as_of = START + timedelta(days=299)
    s = snap(tmp_path, "dupe", rows + [rows[-1]])
    report = quality.check("BROKENUSDT", frame(s, as_of), as_of)
    assert any("duplicate open_time" in i for i in report.issues)


def test_missing_candle_values_refuse(tmp_path):
    rows = make_rows(300, seed=16)
    rows[-5][4] = ""  # blank close -> NaN after parsing
    as_of = START + timedelta(days=299)
    s = snap(tmp_path, "nan", rows)
    report = quality.check("BROKENUSDT", frame(s, as_of), as_of)
    assert any("NaN in" in i for i in report.issues)


def test_stale_feed_refuses(tmp_path):
    rows = make_rows(300, seed=17)
    as_of = START + timedelta(days=299 + cfg.MAX_STALENESS_DAYS + 2)
    s = snap(tmp_path, "stale", rows)
    report = quality.check("BROKENUSDT", frame(s, as_of), as_of)
    assert any("stale" in i for i in report.issues)


def test_zero_volume_tail_refuses(tmp_path):
    rows = make_rows(300, seed=18)
    for r in rows[-cfg.MAX_ZERO_VOLUME_TAIL:]:
        r[5] = "0.00000000"
    as_of = START + timedelta(days=299)
    s = snap(tmp_path, "novol", rows)
    report = quality.check("BROKENUSDT", frame(s, as_of), as_of)
    assert any("zero-volume" in i for i in report.issues)


def test_frozen_price_refuses_rather_than_reporting_calm(tmp_path):
    """A stuck feed produces zero realised volatility, which would otherwise be
    classified as the calmest regime on record."""
    rows = make_rows(300, seed=19)
    for r in rows[-cfg.VOL_WINDOW_REGIME - 5:]:
        r[1] = r[2] = r[3] = r[4] = "100.00000000"
    as_of = START + timedelta(days=299)
    s = snap(tmp_path, "frozen", rows)
    with pytest.raises(SkillRefusal) as exc:
        regime_stage.classify("BROKENUSDT", frame(s, as_of), as_of)
    assert exc.value.reason_code == "degenerate_volatility"


def test_no_data_at_all_refuses(tmp_path):
    as_of = START - timedelta(days=1)
    s = snap(tmp_path, "empty", make_rows(300, seed=20))
    report = quality.check("BROKENUSDT", frame(s, as_of), as_of)
    assert not report.ok
    assert report.n_candles == 0


# --- refusal propagation ---------------------------------------------------


def test_refusal_of_one_symbol_does_not_refuse_the_others(snapshot, monkeypatch):
    """The late listing has 60 days of history; the others must still decide."""
    from pipeline import orchestrator
    from pipeline.llm_cache import LLMCache
    from pipeline.models import LLMDecision, LLMResponse

    as_of = START + timedelta(days=699)

    def fake_decide(as_of_, reads, cache):
        return (LLMResponse(decisions=tuple(
            LLMDecision(symbol=r.symbol, stance="neutral", sizing_tilt=0.5,
                        rationale="stub") for r, _ in reads)), "cache", "stub-key")

    monkeypatch.setattr(orchestrator.allocation, "decide", fake_decide)
    entries = orchestrator.run_as_of(as_of, LLMCache("replay", snapshot), snapshot)

    by_symbol = {e.symbol: e for e in entries}
    assert by_symbol["LATEUSDT"].record_type == "refusal"
    assert by_symbol["LATEUSDT"].refusal.reason_code.startswith("insufficient_history")
    assert by_symbol["BTCUSDT"].record_type == "decision"
    assert by_symbol["BTCUSDT"].decision is not None


def test_allocation_failure_refuses_instead_of_defaulting_to_neutral(snapshot, monkeypatch):
    from pipeline import orchestrator
    from pipeline.llm_cache import LLMCache

    as_of = START + timedelta(days=699)

    def broken_decide(as_of_, reads, cache):
        raise SkillRefusal("allocation", "invalid_llm_output", "schema failed twice")

    monkeypatch.setattr(orchestrator.allocation, "decide", broken_decide)
    entries = orchestrator.run_as_of(as_of, LLMCache("replay", snapshot), snapshot)

    assert all(e.record_type == "refusal" for e in entries)
    assert all(e.decision is None for e in entries)
    codes = {e.refusal.reason_code for e in entries}
    assert "invalid_llm_output" in codes
