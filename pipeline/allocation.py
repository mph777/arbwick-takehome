"""Stage 3 - the allocation agent. One model call per as-of date.

What the model decides:  stance, sizing tilt, and the rationale, given the
                         numbers the deterministic stages produced.
What it must not decide:  the regime, any number, whether a date is refusable,
                          and the risk limits. Those are computed or clamped in
                          code and are not negotiable by argument.

Structure is forced through a tool schema rather than parsed out of prose, and
the result is re-validated against the Pydantic contract. A response that does
not validate gets exactly one reprompt carrying the validation error, and then
the date is refused. It is never defaulted to `neutral`: a silent fallback to a
plausible stance when the agent layer failed is precisely the behaviour the
brief is testing for.
"""

from __future__ import annotations

import os
from datetime import date

from pydantic import ValidationError

import config as cfg
from pipeline import constraints
from pipeline.llm_cache import LLMCache, canonical, request_key
from pipeline.models import LLMResponse, RegimeOutput, RiskOutput, SkillRefusal

STAGE = "allocation"

SYSTEM_PROMPT = """You are an allocation agent inside a quantitative pipeline.

Two deterministic skills have already run. You receive their output as JSON and
nothing else. There is no market data, no news, and no price history available
to you, and you must not act as if there were.

For each symbol you output exactly three things:
  stance       one of risk_on, neutral, risk_off
  sizing_tilt  a number in [0.0, 1.0], the fraction of the mandate's risk budget
  rationale    one or two sentences, referring only to the figures you were given

You do not decide:
  - the regime label, or any number in the input - they are computed, not opinions
  - whether the data was good enough to act on - a date that reached you already
    passed that gate
  - the risk limits - the following are enforced in code after you answer, and a
    proposal that breaches one is overridden and logged as an override:
{rules}

Guidance: treat the volatility percentile as the state of the tape, the drawdown
as how much room the position has already lost, and the expected shortfall as
what a bad day costs from here. Prefer a lower tilt when these disagree. Do not
invent figures, do not reference anything outside the payload, and do not
apologise or hedge in the rationale - state the read."""


TOOL_SCHEMA = {
    "name": "submit_allocation",
    "description": "Submit one allocation decision per symbol in the payload.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "stance": {"type": "string", "enum": list(cfg.STANCES)},
                        "sizing_tilt": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        "rationale": {"type": "string"},
                    },
                    "required": ["symbol", "stance", "sizing_tilt", "rationale"],
                },
            }
        },
        "required": ["decisions"],
    },
}


def system_prompt() -> str:
    return SYSTEM_PROMPT.format(
        rules="\n".join(f"      {r}" for r in constraints.RULES_FOR_PROMPT)
    )


def build_payload(as_of: date, reads: list[tuple[RegimeOutput, RiskOutput]]) -> dict:
    """The exact JSON the model sees.

    Floats are rounded to six decimals so that the cache key is stable across
    platforms and pandas versions - otherwise a last-bit difference in a
    volatility estimate silently invalidates every cached response.
    """
    def r(x: float) -> float:
        return round(float(x), 6)

    return {
        "as_of": as_of.isoformat(),
        "symbols": [
            {
                "symbol": reg.symbol,
                "regime": reg.regime,
                "trend_return_20d": r(reg.trend_return),
                "realized_vol_20d_annualised": r(reg.realized_vol),
                "vol_percentile_20d": r(reg.vol_percentile),
                "drawdown_from_ath": r(risk.drawdown_from_ath),
                "realized_vol_30d_annualised": r(risk.realized_vol),
                "vol_percentile_30d": r(risk.vol_percentile),
                "expected_shortfall_95_1d": r(risk.es_95_1d),
                "es_window_days": risk.es_window_days,
            }
            for reg, risk in reads
        ],
    }


def _extract_tool_input(response) -> dict:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TOOL_SCHEMA["name"]:
            return block.input
    raise ValueError("model returned no submit_allocation tool call")


def _serialise(response) -> dict:
    return {
        "id": response.id,
        "model": response.model,
        "stop_reason": response.stop_reason,
        "usage": {"input_tokens": response.usage.input_tokens,
                  "output_tokens": response.usage.output_tokens},
        "content": [b.model_dump() for b in response.content],
    }


def _deserialise(raw: dict) -> dict:
    for block in raw["content"]:
        if block.get("type") == "tool_use" and block.get("name") == TOOL_SCHEMA["name"]:
            return block["input"]
    raise ValueError("cached response contains no submit_allocation tool call")


def decide(
    as_of: date,
    reads: list[tuple[RegimeOutput, RiskOutput]],
    cache: LLMCache,
) -> tuple[LLMResponse, str, str]:
    """Returns (validated response, "cache"|"live", cache key)."""
    if not reads:
        raise SkillRefusal(STAGE, "no_valid_inputs",
                           "every symbol was refused upstream; nothing to allocate")

    sys_prompt = system_prompt()
    payload = build_payload(as_of, reads)
    key = request_key(sys_prompt, TOOL_SCHEMA, payload)

    cached = cache.get(key)
    if cached is not None:
        return _validate(_deserialise(cached), payload), "cache", key

    if not cache.allows_network():
        from pipeline.llm_cache import CacheMiss
        raise CacheMiss(key, as_of.isoformat())

    raw, response_obj = _call_live(sys_prompt, payload)
    cache.put(key, {"system": sys_prompt, "tool": TOOL_SCHEMA, "payload": payload},
              _serialise(response_obj))
    return raw, "live", key


def _validate(tool_input: dict, payload: dict) -> LLMResponse:
    parsed = LLMResponse.model_validate(tool_input)
    expected = {s["symbol"] for s in payload["symbols"]}
    got = {d.symbol for d in parsed.decisions}
    if got != expected:
        raise ValidationError.from_exception_data(
            "LLMResponse",
            [{"type": "value_error", "loc": ("decisions",),
              "input": sorted(got),
              "ctx": {"error": f"expected decisions for {sorted(expected)}, got {sorted(got)}"}}],
        )
    return parsed


def _call_live(sys_prompt: str, payload: dict) -> tuple[LLMResponse, object]:
    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SkillRefusal(STAGE, "no_api_key",
                           "live mode requested but ANTHROPIC_API_KEY is not set")

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": canonical(payload)}]
    last_error = ""

    for attempt in range(cfg.LLM_MAX_RETRIES + 1):
        response = client.messages.create(
            model=cfg.LLM_MODEL,
            max_tokens=cfg.LLM_MAX_TOKENS,
            temperature=cfg.LLM_TEMPERATURE,
            system=sys_prompt,
            tools=[TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": TOOL_SCHEMA["name"]},
            messages=messages,
        )
        try:
            return _validate(_extract_tool_input(response), payload), response
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)[:800]
            if attempt == cfg.LLM_MAX_RETRIES:
                break
            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content":
                    f"Your submission failed schema validation: {last_error}\n"
                    f"Resubmit via submit_allocation with one decision per symbol "
                    f"in the payload and nothing else."},
            ]

    raise SkillRefusal(
        STAGE, "invalid_llm_output",
        f"model output failed contract validation after "
        f"{cfg.LLM_MAX_RETRIES + 1} attempt(s): {last_error}",
    )
