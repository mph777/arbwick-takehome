"""The clamp layer: what the LLM proposes, code disposes.

The boundary this take-home is really about. The model is allowed to weigh a
small set of numbers it has been handed and pick a stance and a tilt with a
reason. It is not allowed to decide anything that has a defensible mechanical
answer, and it is not allowed to be the last word on risk limits.

Every rule is a pure function of (regime, risk, proposal) with a stable id.
Nothing here consults the model's rationale - a persuasive explanation cannot
buy an exemption, which is the point.
"""

from __future__ import annotations

import math

import config as cfg
from pipeline.models import AppliedConstraint, FinalDecision, LLMDecision, RegimeOutput, RiskOutput

STANCE_ORDER = ("risk_off", "neutral", "risk_on")


def _downgrade(stance: str) -> str:
    """One notch less risk. Minimal intervention: the clamp corrects, it does
    not take over the decision."""
    i = STANCE_ORDER.index(stance)
    return STANCE_ORDER[max(0, i - 1)]


def _quantise(tilt: float) -> float:
    """Round DOWN to the tilt grid. A model that emits 0.37 is expressing
    precision it does not have; rounding down also means quantisation can never
    increase risk.

    The trailing `round(..., 10)` is not cosmetic. `6 * 0.05` is
    0.30000000000000004 in binary floating point, which is strictly greater than
    the 0.30 ceiling this function is supposed to respect - so without it the
    "quantisation cannot increase risk" property is false for a third of the
    grid. A property test over the whole grid is what surfaced it.
    """
    steps = math.floor(round(tilt / cfg.TILT_QUANTUM, 9))
    return min(1.0, max(0.0, round(steps * cfg.TILT_QUANTUM, 10)))


RULES_FOR_PROMPT = [
    f"R1 in bear_high_vol with a 30d volatility percentile above "
    f"{cfg.C_EXTREME_VOL_PCTL:.2f}, risk_on is not available",
    f"R2 with a drawdown at or below {cfg.C_DEEP_DRAWDOWN:.0%}, risk_on is not available",
    "R3 sizing_tilt ceilings by stance: "
    + ", ".join(f"{k} <= {v:.2f}" for k, v in cfg.TILT_CEILING_BY_STANCE.items()),
    f"R4 with a drawdown at or below {cfg.C_DEEP_DRAWDOWN:.0%}, "
    f"sizing_tilt is capped at {cfg.C_DEEP_DRAWDOWN_TILT_CAP:.2f}",
    f"R5 with 1d ES95 at or below {cfg.C_SEVERE_ES:.0%}, "
    f"sizing_tilt is capped at {cfg.C_SEVERE_ES_TILT_CAP:.2f}",
    f"R6 sizing_tilt is rounded down to the nearest {cfg.TILT_QUANTUM:.2f}",
]


def apply(regime: RegimeOutput, risk: RiskOutput, proposal: LLMDecision) -> FinalDecision:
    if proposal.symbol != regime.symbol:
        raise ValueError(f"proposal is for {proposal.symbol}, regime for {regime.symbol}")
    if regime.as_of != risk.as_of:
        raise ValueError("regime and risk payloads disagree on the as-of date")

    stance = proposal.stance
    tilt = proposal.sizing_tilt
    applied: list[AppliedConstraint] = []

    def record(rule_id: str, description: str, before, after) -> None:
        applied.append(AppliedConstraint(rule_id=rule_id, description=description,
                                         before=str(before), after=str(after)))

    # --- stance vetoes -----------------------------------------------------
    if (regime.regime == "bear_high_vol"
            and risk.vol_percentile > cfg.C_EXTREME_VOL_PCTL
            and stance == "risk_on"):
        new = _downgrade(stance)
        record("R1_EXTREME_VOL_BEAR",
               f"bear_high_vol with vol percentile {risk.vol_percentile:.2f} > "
               f"{cfg.C_EXTREME_VOL_PCTL:.2f} forbids risk_on",
               stance, new)
        stance = new

    if risk.drawdown_from_ath <= cfg.C_DEEP_DRAWDOWN and stance == "risk_on":
        new = _downgrade(stance)
        record("R2_DEEP_DRAWDOWN_STANCE",
               f"drawdown {risk.drawdown_from_ath:.1%} at or below "
               f"{cfg.C_DEEP_DRAWDOWN:.0%} forbids risk_on",
               stance, new)
        stance = new

    # --- tilt ceilings (depend on the FINAL stance) ------------------------
    ceiling = cfg.TILT_CEILING_BY_STANCE[stance]
    if tilt > ceiling:
        record("R3_STANCE_TILT_CEILING",
               f"stance {stance} caps sizing_tilt at {ceiling:.2f}", tilt, ceiling)
        tilt = ceiling

    if risk.drawdown_from_ath <= cfg.C_DEEP_DRAWDOWN and tilt > cfg.C_DEEP_DRAWDOWN_TILT_CAP:
        record("R4_DEEP_DRAWDOWN_TILT",
               f"drawdown {risk.drawdown_from_ath:.1%} caps sizing_tilt at "
               f"{cfg.C_DEEP_DRAWDOWN_TILT_CAP:.2f}",
               tilt, cfg.C_DEEP_DRAWDOWN_TILT_CAP)
        tilt = cfg.C_DEEP_DRAWDOWN_TILT_CAP

    if risk.es_95_1d <= cfg.C_SEVERE_ES and tilt > cfg.C_SEVERE_ES_TILT_CAP:
        record("R5_SEVERE_ES_TILT",
               f"1d ES95 {risk.es_95_1d:.2%} at or below {cfg.C_SEVERE_ES:.0%} "
               f"caps sizing_tilt at {cfg.C_SEVERE_ES_TILT_CAP:.2f}",
               tilt, cfg.C_SEVERE_ES_TILT_CAP)
        tilt = cfg.C_SEVERE_ES_TILT_CAP

    quantised = _quantise(tilt)
    if quantised != tilt:
        record("R6_TILT_QUANTISE",
               f"sizing_tilt rounded down to the {cfg.TILT_QUANTUM:.2f} grid",
               tilt, quantised)
        tilt = quantised

    return FinalDecision(
        symbol=proposal.symbol,
        as_of=regime.as_of,
        stance=stance,
        sizing_tilt=tilt,
        rationale=proposal.rationale,
        llm_stance=proposal.stance,
        llm_sizing_tilt=proposal.sizing_tilt,
        constraints_applied=tuple(applied),
    )
