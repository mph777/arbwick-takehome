# ArbWick take-home — regime → risk → allocation

Three-stage pipeline over a committed snapshot of Binance daily klines.
Stages 1 and 2 are deterministic code. Stage 3 is a single LLM call whose output
is validated against a contract and clamped by rules the model cannot argue with.

## Reproducing the committed decision log — no API key needed

```bash
pip install -r requirements.txt
python -m pytest -q                       # 63 tests, offline
python -m pipeline.orchestrator --weekly --check
```

`--check` re-runs every Friday in the sample from the committed snapshot and
diffs the result against `logs/decisions.jsonl`. It runs in `replay` mode, which
never touches the network: every model response is committed under `llm_cache/`,
content-addressed by a hash of the model id, sampling parameters, system prompt,
tool schema and the exact payload. See "Reproducibility" in `writeup.md` for why
this is a record rather than a hard-coded answer.

Inspect the log:

```bash
python -m tools.report
```

## Regenerating from scratch (needs network + an API key)

```bash
python data/fetch.py                 # writes data/snapshot/ + manifest.json
python data/verify.py                # hard checks gate everything downstream
export ANTHROPIC_API_KEY=...
python -m pipeline.orchestrator --weekly --mode live
```

Single date:

```bash
python -m pipeline.orchestrator --as-of 2025-03-14
```

## Layout

```
config.py                 every parameter that can change a decision
data/fetch.py             paginated kline fetch + empirical late-symbol discovery
data/verify.py            snapshot verification, hard/soft findings, exits non-zero
pipeline/loader.py        the ONE place data is cut at the as-of boundary
pipeline/models.py        strict Pydantic contracts between stages
pipeline/quality.py       stage 0 — is the history at t usable at all
pipeline/regime.py        stage 1 — 2x2 rolling-statistic regime
pipeline/risk.py          stage 2 — drawdown, vol percentile, 365d ES95
pipeline/allocation.py    stage 3 — one tool-constrained model call
pipeline/constraints.py   the clamp layer, one pure function per rule
pipeline/llm_cache.py     content-addressed request cache (replay/live/refresh)
pipeline/orchestrator.py  as-of runner, per-symbol refusal, decision log
tools/report.py           the figures quoted in the writeup
logs/decisions.jsonl      the committed demo run
NOTES.md                  what the AI tooling got wrong while building this
writeup.md                architecture, verification, failure modes, cost
```

## The decision log

One JSON line per `(as-of date, symbol)`, `record_type` either `decision` or
`refusal`. Every line carries its own provenance — snapshot hash, git commit,
model id, prompt hash, parameter hash, the as-of cutoff in milliseconds — so a
line can be audited without trusting the run that produced it. Decision lines
carry the full Stage 1 and Stage 2 output, the model's original proposal, the
final stance and tilt, and every constraint that fired.
