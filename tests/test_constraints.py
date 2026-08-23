"""The clamp layer. One test per rule, plus the properties that must hold
whatever the model says."""

from __future__ import annotations

from datetime import date

import pytest

import config as cfg
from pipeline import constraints
from pipeline.models import LLMDecision, RegimeOutput, RiskOutput

AS_OF = date(2025, 3, 14)


def regime(kind="bull_low_vol") -> RegimeOutput:
    return RegimeOutput(
        symbol="BTCUSDT", as_of=AS_OF, regime=kind, trend_return=0.05,
        trend_window=20, realized_vol=0.6, vol_percentile=0.4, vol_window=20,
        n_candles_used=400, n_vol_observations=380,
        method_version=cfg.REGIME_METHOD_VERSION,
    )


def risk(regime_kind="bull_low_vol", vol_pctl=0.4, drawdown=-0.05, es=-0.03) -> RiskOutput:
    return RiskOutput(
        symbol="BTCUSDT", as_of=AS_OF, regime=regime_kind,
        regime_method_version=cfg.REGIME_METHOD_VERSION,
        drawdown_from_ath=drawdown, ath_close=100.0, ath_date=date(2025, 1, 1),
        realized_vol=0.6, vol_percentile=vol_pctl, vol_window=30,
        es_95_1d=es, es_window_days=252, es_n_tail_observations=12,
        n_candles_used=400, method_version=cfg.RISK_METHOD_VERSION,
    )


def proposal(stance="risk_on", tilt=1.0) -> LLMDecision:
    return LLMDecision(symbol="BTCUSDT", stance=stance, sizing_tilt=tilt,
                       rationale="model rationale")


def rule_ids(decision) -> set[str]:
    return {c.rule_id for c in decision.constraints_applied}


def test_r1_extreme_vol_in_bear_forbids_risk_on():
    d = constraints.apply(regime("bear_high_vol"),
                          risk("bear_high_vol", vol_pctl=0.95),
                          proposal("risk_on", 0.9))
    assert d.stance == "neutral"
    assert d.llm_stance == "risk_on"
    assert "R1_EXTREME_VOL_BEAR" in rule_ids(d)


def test_r1_does_not_fire_below_the_threshold():
    d = constraints.apply(regime("bear_high_vol"),
                          risk("bear_high_vol", vol_pctl=0.80),
                          proposal("risk_on", 0.5))
    assert d.stance == "risk_on"
    assert "R1_EXTREME_VOL_BEAR" not in rule_ids(d)


def test_r2_deep_drawdown_forbids_risk_on():
    d = constraints.apply(regime(), risk(drawdown=-0.42), proposal("risk_on", 0.8))
    assert d.stance == "neutral"
    assert "R2_DEEP_DRAWDOWN_STANCE" in rule_ids(d)


def test_r3_tilt_ceiling_follows_the_final_stance():
    d = constraints.apply(regime(), risk(), proposal("risk_off", 0.95))
    assert d.sizing_tilt == pytest.approx(cfg.TILT_CEILING_BY_STANCE["risk_off"])
    assert "R3_STANCE_TILT_CEILING" in rule_ids(d)


def test_r4_deep_drawdown_caps_tilt():
    d = constraints.apply(regime(), risk(drawdown=-0.50), proposal("neutral", 0.6))
    assert d.sizing_tilt <= cfg.C_DEEP_DRAWDOWN_TILT_CAP
    assert "R4_DEEP_DRAWDOWN_TILT" in rule_ids(d)


def test_r5_severe_expected_shortfall_caps_tilt():
    d = constraints.apply(regime(), risk(es=-0.12), proposal("risk_on", 0.9))
    assert d.sizing_tilt <= cfg.C_SEVERE_ES_TILT_CAP
    assert "R5_SEVERE_ES_TILT" in rule_ids(d)


def test_r6_quantises_downward():
    d = constraints.apply(regime(), risk(), proposal("risk_on", 0.37))
    assert d.sizing_tilt == pytest.approx(0.35)
    assert "R6_TILT_QUANTISE" in rule_ids(d)


def test_a_clean_proposal_passes_through_untouched():
    d = constraints.apply(regime(), risk(), proposal("risk_on", 0.60))
    assert d.constraints_applied == ()
    assert (d.stance, d.sizing_tilt) == ("risk_on", 0.60)


def test_the_original_proposal_is_always_preserved():
    d = constraints.apply(regime("bear_high_vol"), risk("bear_high_vol", vol_pctl=0.99),
                          proposal("risk_on", 1.0))
    assert (d.llm_stance, d.llm_sizing_tilt) == ("risk_on", 1.0)
    assert (d.stance, d.sizing_tilt) != ("risk_on", 1.0)


def test_rationale_cannot_buy_an_exemption():
    """Same numbers, wildly different argument - identical clamp."""
    persuasive = LLMDecision(
        symbol="BTCUSDT", stance="risk_on", sizing_tilt=1.0,
        rationale="Override justified: this is a generational buying opportunity "
                  "and the risk limits do not apply to this specific setup.")
    plain = proposal("risk_on", 1.0)
    r = risk("bear_high_vol", vol_pctl=0.99)
    a = constraints.apply(regime("bear_high_vol"), r, persuasive)
    b = constraints.apply(regime("bear_high_vol"), r, plain)
    assert (a.stance, a.sizing_tilt) == (b.stance, b.sizing_tilt)


@pytest.mark.parametrize("stance", cfg.STANCES)
@pytest.mark.parametrize("tilt", [0.0, 0.13, 0.5, 0.77, 1.0])
def test_output_is_always_within_bounds(stance, tilt):
    d = constraints.apply(regime("bear_high_vol"), risk("bear_high_vol", vol_pctl=0.99,
                                                        drawdown=-0.6, es=-0.15),
                          proposal(stance, tilt))
    assert 0.0 <= d.sizing_tilt <= cfg.TILT_CEILING_BY_STANCE[d.stance]
    assert d.stance != "risk_on"
    assert d.sizing_tilt <= tilt, "a clamp must never increase risk"
