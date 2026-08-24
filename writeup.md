# Regime → risk → allocation: design notes

Numbers from `python -m tools.report`; build mistakes in `NOTES.md`.

## 1. Code vs. LLM

Code handles everything with a clear mechanical answer: regime label, risk metrics, whether to refuse a symbol, hard position limits. The LLM gets one job: three risk measures often point in different directions — translate them into a single stance and explain why.

The LLM sees only the JSON output of the first two stages — no prices, no history. Its output format is enforced by a schema; if validation fails, it gets one retry, then the symbol is refused. There is no fallback answer. Six hard rules in `constraints.py` cap the final position regardless of what the LLM proposes — the rationale text cannot change the cap.

All overrides are logged with the original proposal next to the final decision. In the demo: 86 of 413 decisions were modified. R5 fired 66×, R4 36×, R2 31×, R3 25×. **R1 and R6 never fired** — R1 is the rule the design started from. A rule that never fires on real data is only as good as its unit test, and that is worth stating plainly.

## 2. Point-in-time

A Friday decision uses only candles that closed before midnight of the following day — nothing later. The time filter runs once, in `loader.py`. No stage ever sees data beyond that cut. A test verifies this by inserting 120 fake future candles with extreme prices and checking the output does not change.

## 3. Reproducibility

`python -m pipeline.orchestrator --weekly --check` regenerates all 748 lines and reports any differences by field name. LLM responses are saved in `llm_cache/` and keyed on model id, prompt, schema, and the exact input. Default mode is `replay` — no API key needed. If any input number changes, the cached response no longer matches and the run fails with an error rather than returning an outdated answer.

Two fields are excluded from the comparison: `git_commit` and `llm_source` (which shows `live` when the cache was written and `cache` on every replay). They describe how the run happened, not what the decision was.

## 4. Data

All checks pass: 1 308 gap-free candles for BTC/ETH/SOL, 415 for HOMEUSDT. Minor issues like missing days are reported but left as-is — fixing them automatically would hide the exact conditions the pipeline is supposed to detect and refuse.

HOMEUSDT was picked as the late-listing symbol from 482 candidates. Its history gate opens on 2026-06-12 (a Friday), so the log shows 179 refusals then 8 decisions as the gate opens.

One technical note: Git on Windows silently changes line endings in text files, which alters the file checksum without changing what you see. `.gitattributes` disables this for the data, cache, and log folders.

## 5. What the pipeline catches and what it misses

**335 of 748 lines are refusals** — by design. Breakdown: 127 `no_data`, 33 `regime/insufficient_history`, 16 `risk/insufficient_history`, 159 `insufficient_history_for_es`.

Known gaps:
- A price that is wrong but looks plausible passes every check.
- The symbol universe is fixed at snapshot time — a symbol that delisted mid-period would still be included.
- Regime labels flip too often (61 changes in 135 BTC decisions) because the trend signal sits near zero. Smoothing it would require keeping state between weeks, which breaks the point-in-time guarantee.
- ES weights every day in the 365-day window equally, so it is slow to react when the market changes.

ES uses 365 days rather than the TradFi convention of 252 because crypto trades every day of the year — 252 would be inconsistent with the annualisation formula used in the same module.

## 6. Before using this with real capital

- Make `--check` a CI gate
- Add a test for every known failure type, checking the specific error code not just that something failed
- Run against live data for a quarter with no capital before going live
- Watch the refusal rate — if it drops toward zero, the gate is probably broken, not improving
- R1 and R6 never fired on real data; either prove they work or remove them

## 7. Cost

~$0.0034 per weekly run (one call covering all symbols, ~1 638 input / 353 output tokens). Full three-year backfill: 135 calls, **$0.46** total. Replay is free. Nothing to optimise at this cadence.

## 8. What the AI tooling got wrong

Details for each item are in `NOTES.md`.

1. **Floating-point rounding broke a risk ceiling** — a rounding formula returned a value slightly above the limit it was enforcing, and its own comment said the opposite. Caught by a property test.
2. **A test fixture that tested the wrong date** — the test ran on a Wednesday; the cache only had Fridays, so it failed loudly. Lucky that it did.
3. **Repeated attempts to replace refusals with default answers** — `except ValidationError: return neutral` reads as safe error handling but is exactly the wrong behaviour here.
4. **Point-in-time filtering was duplicated in four places** — consolidated to one.
5. **Two definitions of "a year" in one module** — `sqrt(365)` for one calculation, 252 days for another. Invisible until someone asked why 252.
6. **Late-symbol selection picked a candidate with 15 candles** — the ranking key was not comparable across symbols with different history lengths.
7. **Code written against an outdated API** — `temperature=` was removed from the SDK; a retired model id was used. Both lived in the one function the offline tests never execute, and surfaced 52 dates into a live run. Fixed by a test that checks the installed SDK's call signature without making a real API call.
8. **The reproducibility check always failed on first use** — it compared a field that is intentionally different between a live run and a replay. Fixed by excluding that field and adding a test for both sides.
