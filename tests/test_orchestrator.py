"""End-to-end: weekly run, decision log shape, and log-level reproducibility."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

import config as cfg
from pipeline import allocation, orchestrator
from pipeline.llm_cache import LLMCache, request_key
from tests.conftest import START
from tests.test_llm_cache import stub_response


SAMPLE_END = START + timedelta(days=699)
LAST_FRIDAY = [d for d in [SAMPLE_END - timedelta(days=i) for i in range(7)]
               if d.weekday() == 4][0]


@pytest.fixture
def primed_cache(snapshot, tmp_path):
    """Record a stub response for every Friday, exactly as a live run would."""
    cache = LLMCache("replay", tmp_path / "cache")
    for d in orchestrator.fridays(START, SAMPLE_END):
        reads = []
        for symbol in orchestrator.universe(snapshot):
            from pipeline import regime as rg, risk as rk
            from pipeline.loader import as_of_cutoff_ms, load_as_of
            from pipeline.models import SkillRefusal
            from pipeline import quality
            df = load_as_of(symbol, as_of_cutoff_ms(d), snapshot)
            if not quality.check(symbol, df, d).ok:
                continue
            try:
                reg = rg.classify(symbol, df, d)
                reads.append((reg, rk.assess(symbol, df, reg)))
            except SkillRefusal:
                continue
        if not reads:
            continue
        payload = allocation.build_payload(d, reads)
        key = request_key(allocation.system_prompt(), allocation.TOOL_SCHEMA, payload)
        cache.put(key, {"payload": payload}, stub_response(payload))
    return cache


def test_weekly_run_produces_decisions_and_honest_refusals(snapshot, primed_cache):
    entries = []
    for d in orchestrator.fridays(START, SAMPLE_END):
        entries.extend(orchestrator.run_as_of(d, primed_cache, snapshot))

    decisions = [e for e in entries if e.record_type == "decision"]
    refusals = [e for e in entries if e.record_type == "refusal"]
    assert decisions and refusals, "a real sample contains both"

    # No date before the ES window can carry a decision.
    earliest = min(e.provenance.as_of for e in decisions)
    assert (earliest - START).days >= cfg.MIN_CANDLES_ES - 1

    # Every refusal names a stage and a reason; none carries a stance.
    for e in refusals:
        assert e.decision is None
        assert e.refusal.stage and e.refusal.reason_code

    # The late listing is refused for its whole thin period, never guessed at.
    late = [e for e in entries if e.symbol == "LATEUSDT"]
    assert late and all(e.record_type == "refusal" for e in late)


def test_log_lines_are_self_describing(snapshot, primed_cache, tmp_path):
    as_of = LAST_FRIDAY
    entries = orchestrator.run_as_of(as_of, primed_cache, snapshot)
    out = tmp_path / "decisions.jsonl"
    orchestrator.write_log(entries, out)

    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert rows
    for row in rows:
        p = row["provenance"]
        for field in ("schema_version", "as_of", "as_of_cutoff_ms",
                      "snapshot_sha256", "model_id", "prompt_sha256", "params_sha256"):
            assert p[field], f"{field} missing from provenance"
        if row["record_type"] == "decision":
            assert row["decision"]["rationale"]
            assert row["regime"]["regime"] and row["risk"]["es_95_1d"] <= 0
            assert row["llm_source"] in ("cache", "live")
            assert row["llm_cache_key"]


def test_rerunning_the_same_date_is_byte_identical(snapshot, primed_cache):
    as_of = LAST_FRIDAY
    a = [orchestrator.serialise(e) for e in orchestrator.run_as_of(as_of, primed_cache, snapshot)]
    b = [orchestrator.serialise(e) for e in orchestrator.run_as_of(as_of, primed_cache, snapshot)]
    assert a == b


def test_an_earlier_date_is_unaffected_by_later_ones(snapshot, primed_cache):
    early = orchestrator.fridays(START, START + timedelta(days=500))[-1]
    late = LAST_FRIDAY
    first = [orchestrator.serialise(e) for e in orchestrator.run_as_of(early, primed_cache, snapshot)]
    orchestrator.run_as_of(late, primed_cache, snapshot)
    again = [orchestrator.serialise(e) for e in orchestrator.run_as_of(early, primed_cache, snapshot)]
    assert first == again


def test_fridays_are_fridays():
    days = orchestrator.fridays(date(2023, 1, 1), date(2023, 3, 1))
    assert days[0] == date(2023, 1, 6)
    assert all(d.weekday() == 4 for d in days)
    assert all((b - a).days == 7 for a, b in zip(days, days[1:]))
