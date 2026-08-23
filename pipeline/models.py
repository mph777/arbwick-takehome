"""Typed contracts between stages.

Every model is `extra="forbid"` and frozen: a stage that receives a payload with
an unexpected field, a missing field, or an out-of-range value raises
`pydantic.ValidationError` instead of coercing it into something plausible.
That is deliberate - the failure mode this pipeline is built to avoid is a stage
quietly repairing an upstream defect.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Stance = Literal["risk_on", "neutral", "risk_off"]
Regime = Literal["bull_low_vol", "bull_high_vol", "bear_low_vol", "bear_high_vol"]
RecordType = Literal["decision", "refusal"]


class Contract(BaseModel):
    """Base for all inter-stage payloads: strict, immutable, hashable to JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_assignment=True)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class SkillRefusal(Exception):
    """Raised by a skill that cannot stand behind an answer.

    Carries a machine-readable reason code so the decision log can be queried,
    plus human detail so an auditor does not have to re-run anything.
    """

    def __init__(self, stage: str, reason_code: str, detail: str) -> None:
        super().__init__(f"[{stage}] {reason_code}: {detail}")
        self.stage = stage
        self.reason_code = reason_code
        self.detail = detail


class Refusal(Contract):
    stage: str
    reason_code: str
    detail: str


# ---------------------------------------------------------------------------
# Stage 0 - data quality
# ---------------------------------------------------------------------------


class DataQualityReport(Contract):
    symbol: str
    as_of: date
    n_candles: int = Field(ge=0)
    first_date: date | None
    last_date: date | None
    max_consecutive_gap_days: int = Field(ge=0)
    reason_codes: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues


# ---------------------------------------------------------------------------
# Stage 1 - regime
# ---------------------------------------------------------------------------


class RegimeOutput(Contract):
    symbol: str
    as_of: date
    regime: Regime
    trend_return: float
    trend_window: int = Field(gt=0)
    realized_vol: float = Field(ge=0.0)
    vol_percentile: float = Field(ge=0.0, le=1.0)
    vol_window: int = Field(gt=0)
    n_candles_used: int = Field(gt=0)
    n_vol_observations: int = Field(gt=0)
    method_version: str

    @field_validator("trend_return", "realized_vol")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("non-finite statistic")
        return v


# ---------------------------------------------------------------------------
# Stage 2 - risk
# ---------------------------------------------------------------------------


class RiskOutput(Contract):
    symbol: str
    as_of: date
    # Echoed from Stage 1 so an auditor can prove the risk read was computed
    # conditional on the regime that was actually published for that date.
    regime: Regime
    regime_method_version: str

    drawdown_from_ath: float = Field(le=0.0)
    ath_close: float = Field(gt=0.0)
    ath_date: date

    realized_vol: float = Field(ge=0.0)
    vol_percentile: float = Field(ge=0.0, le=1.0)
    vol_window: int = Field(gt=0)

    es_95_1d: float = Field(le=0.0)
    es_window_days: int = Field(gt=0)
    es_n_tail_observations: int = Field(gt=0)

    n_candles_used: int = Field(gt=0)
    method_version: str


# ---------------------------------------------------------------------------
# Stage 3 - allocation
# ---------------------------------------------------------------------------


class LLMDecision(Contract):
    """Exactly what the model is permitted to emit, before any clamping."""

    symbol: str
    stance: Stance
    sizing_tilt: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=600)


class LLMResponse(Contract):
    decisions: tuple[LLMDecision, ...] = Field(min_length=1)


class AppliedConstraint(Contract):
    rule_id: str
    description: str
    before: str
    after: str


class FinalDecision(Contract):
    symbol: str
    as_of: date
    stance: Stance
    sizing_tilt: float = Field(ge=0.0, le=1.0)
    rationale: str
    llm_stance: Stance
    llm_sizing_tilt: float = Field(ge=0.0, le=1.0)
    constraints_applied: tuple[AppliedConstraint, ...] = ()


# ---------------------------------------------------------------------------
# Decision log
# ---------------------------------------------------------------------------


class RunProvenance(Contract):
    """Everything needed to reconstruct a line without trusting the runner."""

    schema_version: str
    as_of: date
    as_of_cutoff_ms: int
    snapshot_sha256: str
    git_commit: str | None
    model_id: str
    prompt_sha256: str
    params_sha256: str


class DecisionLogEntry(Contract):
    record_type: RecordType
    symbol: str
    provenance: RunProvenance

    # populated when record_type == "decision"
    regime: RegimeOutput | None = None
    risk: RiskOutput | None = None
    decision: FinalDecision | None = None
    llm_source: Literal["cache", "live"] | None = None
    llm_cache_key: str | None = None

    # populated when record_type == "refusal"
    refusal: Refusal | None = None

    @field_validator("refusal")
    @classmethod
    def _refusal_shape(cls, v, info):
        if info.data.get("record_type") == "refusal" and v is None:
            raise ValueError("refusal records must carry a Refusal")
        return v
