"""The cache must be a record of what was asked, never a way of pretending."""

from __future__ import annotations

from datetime import timedelta

import pytest

import config as cfg
from pipeline import allocation
from pipeline.llm_cache import CacheMiss, LLMCache, request_key
from pipeline.loader import as_of_cutoff_ms, load_as_of
from pipeline import regime as regime_stage, risk as risk_stage


def reads_for(snapshot, symbol, as_of):
    df = load_as_of(symbol, as_of_cutoff_ms(as_of), snapshot)
    reg = regime_stage.classify(symbol, df, as_of)
    return [(reg, risk_stage.assess(symbol, df, reg))]


def stub_response(payload: dict) -> dict:
    return {"id": "msg_stub", "model": cfg.LLM_MODEL, "stop_reason": "tool_use",
            "usage": {"input_tokens": 500, "output_tokens": 120},
            "content": [{"type": "tool_use", "id": "toolu_stub",
                         "name": allocation.TOOL_SCHEMA["name"],
                         "input": {"decisions": [
                             {"symbol": s["symbol"], "stance": "neutral",
                              "sizing_tilt": 0.5, "rationale": "stub"}
                             for s in payload["symbols"]]}}]}


def test_replay_mode_never_reaches_the_network(clean_symbol, tmp_path):
    snapshot, symbol, as_of = clean_symbol
    cache = LLMCache("replay", tmp_path / "cache")
    with pytest.raises(CacheMiss):
        allocation.decide(as_of, reads_for(snapshot, symbol, as_of), cache)


def test_a_recorded_response_replays_exactly(clean_symbol, tmp_path):
    snapshot, symbol, as_of = clean_symbol
    reads = reads_for(snapshot, symbol, as_of)
    payload = allocation.build_payload(as_of, reads)
    key = request_key(allocation.system_prompt(), allocation.TOOL_SCHEMA, payload)

    cache = LLMCache("replay", tmp_path / "cache")
    cache.put(key, {"payload": payload}, stub_response(payload))

    response, source, used_key = allocation.decide(as_of, reads, cache)
    assert source == "cache" and used_key == key
    assert response.decisions[0].symbol == symbol


def test_changing_an_input_invalidates_the_cached_answer(clean_symbol, tmp_path):
    """The point of content addressing: a different question cannot be answered
    by an old answer."""
    snapshot, symbol, as_of = clean_symbol
    reads = reads_for(snapshot, symbol, as_of)
    payload = allocation.build_payload(as_of, reads)
    key = request_key(allocation.system_prompt(), allocation.TOOL_SCHEMA, payload)

    cache = LLMCache("replay", tmp_path / "cache")
    cache.put(key, {"payload": payload}, stub_response(payload))

    other = reads_for(snapshot, symbol, as_of - timedelta(days=7))
    with pytest.raises(CacheMiss):
        allocation.decide(as_of - timedelta(days=7), other, cache)


def test_prompt_changes_change_the_key(clean_symbol):
    snapshot, symbol, as_of = clean_symbol
    payload = allocation.build_payload(as_of, reads_for(snapshot, symbol, as_of))
    a = request_key("system A", allocation.TOOL_SCHEMA, payload)
    b = request_key("system B", allocation.TOOL_SCHEMA, payload)
    assert a != b


def test_a_response_missing_a_symbol_fails_validation(clean_symbol, tmp_path):
    snapshot, symbol, as_of = clean_symbol
    reads = reads_for(snapshot, symbol, as_of)
    payload = allocation.build_payload(as_of, reads)
    key = request_key(allocation.system_prompt(), allocation.TOOL_SCHEMA, payload)

    bad = stub_response(payload)
    bad["content"][0]["input"]["decisions"][0]["symbol"] = "DOGEUSDT"
    cache = LLMCache("replay", tmp_path / "cache")
    cache.put(key, {"payload": payload}, bad)

    with pytest.raises(Exception):
        allocation.decide(as_of, reads, cache)


def test_payload_floats_are_rounded_for_key_stability(clean_symbol):
    snapshot, symbol, as_of = clean_symbol
    payload = allocation.build_payload(as_of, reads_for(snapshot, symbol, as_of))
    for s in payload["symbols"]:
        for k, v in s.items():
            if isinstance(v, float):
                assert round(v, 6) == v, f"{k} is not rounded"
