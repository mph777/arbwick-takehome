"""Inter-stage contracts reject malformed payloads instead of coercing them."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

import config as cfg
from pipeline import risk as risk_stage
from pipeline.loader import as_of_cutoff_ms, load_as_of
from pipeline.models import LLMResponse, RegimeOutput


def a_regime(**overrides) -> dict:
    base = dict(
        symbol="BTCUSDT", as_of=date(2025, 1, 3), regime="bull_low_vol",
        trend_return=0.05, trend_window=20, realized_vol=0.6, vol_percentile=0.4,
        vol_window=20, n_candles_used=400, n_vol_observations=380,
        method_version=cfg.REGIME_METHOD_VERSION,
    )
    base.update(overrides)
    return base


def test_unknown_field_is_rejected_not_ignored():
    with pytest.raises(ValidationError, match="Extra inputs"):
        RegimeOutput(**a_regime(confidence=0.9))


def test_out_of_range_percentile_is_rejected():
    with pytest.raises(ValidationError):
        RegimeOutput(**a_regime(vol_percentile=1.4))


def test_unknown_regime_label_is_rejected():
    with pytest.raises(ValidationError):
        RegimeOutput(**a_regime(regime="sideways"))


def test_payloads_are_immutable():
    reg = RegimeOutput(**a_regime())
    with pytest.raises(ValidationError):
        reg.regime = "bear_high_vol"


def test_stage_2_rejects_a_dict_instead_of_coercing_it(clean_symbol):
    snapshot, symbol, as_of = clean_symbol
    df = load_as_of(symbol, as_of_cutoff_ms(as_of), snapshot)
    with pytest.raises(TypeError, match="requires a RegimeOutput"):
        risk_stage.assess(symbol, df, a_regime(symbol=symbol, as_of=as_of))


def test_stage_2_rejects_a_regime_for_a_different_symbol(clean_symbol):
    snapshot, symbol, as_of = clean_symbol
    df = load_as_of(symbol, as_of_cutoff_ms(as_of), snapshot)
    reg = RegimeOutput(**a_regime(symbol="ETHUSDT", as_of=as_of))
    with pytest.raises(ValueError, match="asked about"):
        risk_stage.assess(symbol, df, reg)


def test_risk_output_echoes_the_regime_it_was_conditioned_on(clean_symbol):
    from pipeline import regime as regime_stage

    snapshot, symbol, as_of = clean_symbol
    df = load_as_of(symbol, as_of_cutoff_ms(as_of), snapshot)
    reg = regime_stage.classify(symbol, df, as_of)
    rsk = risk_stage.assess(symbol, df, reg)
    assert rsk.regime == reg.regime
    assert rsk.regime_method_version == reg.method_version
    assert rsk.as_of == reg.as_of


def test_llm_response_rejects_an_out_of_band_stance():
    with pytest.raises(ValidationError):
        LLMResponse(decisions=({"symbol": "BTCUSDT", "stance": "max_long",
                                "sizing_tilt": 0.5, "rationale": "x"},))


def test_llm_response_rejects_a_tilt_outside_the_unit_interval():
    with pytest.raises(ValidationError):
        LLMResponse(decisions=({"symbol": "BTCUSDT", "stance": "risk_on",
                                "sizing_tilt": 1.5, "rationale": "x"},))


def test_llm_response_rejects_an_empty_rationale():
    with pytest.raises(ValidationError):
        LLMResponse(decisions=({"symbol": "BTCUSDT", "stance": "risk_on",
                                "sizing_tilt": 0.5, "rationale": ""},))
