# Regime → risk → allocation: design notes

Every figure below comes from `python -m tools.report` against the committed
decision log. Where a decision has a longer story, this points at the file that
tells it: module docstrings carry the per-stage reasoning, and `NOTES.md` carries
what went wrong during the build.

## 1. The code/LLM boundary

Drawn at **verifiability**. Anything with a defensible mechanical answer is code;
the LLM gets what is left, which is weighing several partially-conflicting
readings into one posture and saying why.

| | decided by | why |
|---|---|---|
| Regime label | code | A rolling statistic is reproducible and auditable. An LLM labelling a regime from the same numbers is a slower, non-deterministic threshold. |
| Drawdown, vol percentile, ES95 | code | Arithmetic. |
| Whether a date is answerable | code | The refusal must be the one thing in the chain that cannot be argued with. |
| Risk limits | code | Below. |
| Stance and sizing tilt | **LLM** | Three measures that routinely disagree — a shallow drawdown with a 95th-percentile tape, a deep drawdown with a calm one. Encoding that trade-off as a lookup table means inventing a weighting nobody can defend. |
| Rationale | **LLM** | The artefact an auditor reads to understand why. |

Stage 3 receives Stage 1 and Stage 2 output as JSON and nothing else — no prices,
no history. It cannot recompute anything, so it cannot contradict the
deterministic stages, and it has no data with which to invent a market narrative.

**Forbidden, and how that is enforced.** The regime and every number: structural,
computed upstream, no tool to change them. Whether to refuse: decided before the
model is called, and a refused symbol is *removed from the payload* rather than
passed with a gap to reason around. Its own output shape: forced through a tool
schema, re-validated against the Pydantic contract including that the symbol set
returned equals the set sent, one reprompt on failure, then refusal — never a
fallback to `neutral`. The risk limits: six rules in `pipeline/constraints.py`,
each a pure function of `(regime, risk, proposal)` with a stable id, which never
read the rationale — a persuasive argument cannot buy an exemption, and
`tests/test_constraints.py` asserts exactly that.

Overrides are logged, not silent: every line keeps the model's original stance
and tilt beside the final ones. In the demo run 86 of 413 decisions were
modified, 31 of them in the stance. Which rules did the work is itself a finding:
`R5` (severe ES) fired 66 times, `R4` 36, `R2` 31, `R3` 25 — and **`R1` never
fired**, nor did `R6`. `R1` is the rule this design started from; on real data the
combination it guards never occurred, and the drawdown and tail-loss rules did
the restraining. A rule that has never fired has never been tested outside its
unit test, and I would not offer either as evidence the clamp layer works.

**Orchestration is a plain `for` loop.** The DAG is linear and fixed with one
model call in it. A framework earns its place when control flow is genuinely
dynamic; here it would add a scheduler and a serialisation layer on top of five
function calls and make the leakage boundary harder to see.

## 2. Point-in-time

A decision dated *t* is stamped 00:00:00.000 UTC of *t+1* and may use every daily
candle closed by then — candle *t* included, nothing later in existence. The
boundary is a strict `<` against the next midnight, so it does not depend on the
exchange's `close_time` precision.

The cut happens **once**, in `pipeline/loader.py`. No stage ever receives an
unfiltered frame, so leakage is structurally impossible rather than a property
each stage must independently preserve (it was four copies once — `NOTES.md` §4).
`tests/test_point_in_time.py` defends it two ways: truncation equivalence, and
future invariance — appending 120 candles after *t* at 10× and 0.1× the last
close must leave the as-of-*t* output bit-identical. The second is the one that
catches a window computed before the cut or a percentile ranked over the whole
file.

## 3. Reproducibility

`python -m pipeline.orchestrator --weekly --check` regenerates all 748 lines from
the committed snapshot and cache and diffs them, naming the differing fields.

Two things make that work offline. **The model call is content-addressed and
committed**: keyed on model id, token limit, system prompt, tool schema and the
exact payload, with the response under `llm_cache/`. `replay` is the default and
never touches the network, so a reviewer needs no key. It is a record, not a
hard-coded answer — change a Stage 2 statistic and the key changes, and replay
fails loudly rather than answering a question that was never asked. **Every line
is self-describing**: snapshot hash, git commit, model id, prompt hash, parameter
hash, cache key and the as-of cutoff ride on each one.

Two fields are excluded from the comparison and the distinction matters:
`git_commit`, and `llm_source`, which reads `live` on the run that recorded the
cache and `cache` on every replay. Neither describes the decision — and the cache
key, which identifies request and response exactly, *is* compared. Tests assert
both halves: a live-written log and a replayed one compare equal, and a tampered
tilt still fails.

## 4. Data verification

`data/fetch.py` writes each kline field verbatim as the string the exchange
returned, so the file hash means "what Binance said" and re-fetching is
byte-stable. It pages at 1000 candles, de-duplicates on `open_time` because
`startTime` is inclusive, and drops any candle unclosed at fetch time.

`data/verify.py` splits findings in two. **Hard** (exits non-zero): manifest
hash mismatch, duplicate or non-monotonic timestamps, a kline not spanning
exactly 86 399 999 ms, non-positive prices, impossible candle geometry, negative
volume, taker-buy volume above total, NaN, data outside the mandated window, a
late symbol contradicting its listing probe. **Soft** (reported, never patched):
missing days, gap runs, zero-volume days, flat-range days, date-set differences
between symbols.

The split is deliberate. A soft finding is a real property of exchange history,
and forward-filling it at fetch time would hide from the pipeline the exact
condition it exists to refuse on. Irregularities survive into the snapshot and
are handled at run time by `pipeline/quality.py`.

**What the fetch found:** nothing irregular. The three core symbols return 1308
candles over 2023-01-01 → 2026-07-31 with no missing days, no gaps, no
zero-volume days and identical date sets; HOMEUSDT returns 415 from its listing,
equally clean. Every hard check passes — cleaner than the brief implies, which
makes the checks the reason that claim can be *made* rather than assumed. The
broken-data tests exercise every refusal path against synthetic snapshots
precisely because the real one never triggers them.

**The late symbol was chosen from the data** — 482 USDT spot pairs probed, 134
listed after 2025-06-01, 9 with both an unbroken history and enough candles to
reach the ES gate. Selected: **HOMEUSDT**, first candle 2025-06-12, 415 candles.
Its 366th falls on 2026-06-12, itself a Friday, so the log carries 179 refusals
and then 8 decisions with the transition landing exactly on a decision date. The
ranking key — median daily quote volume over a *fixed* trailing slice of the
window — went through two wrong versions first, both instructive; `NOTES.md` §6.

One transport detail belongs here because it defeats the integrity check without
touching the data: Git for Windows rewrites LF to CRLF on checkout, leaving every
CSV visually identical with a different sha256. A committed `.gitattributes`
disables it for `data/snapshot/`, `llm_cache/` and `logs/`.

## 5. Failure modes

**Caught.** Three distinct history floors, each with its own reason code; gaps
inside the consumed window; a stale feed; duplicate or non-monotonic timestamps;
NaN and non-positive prices; a frozen feed, which yields zero realised volatility
and would otherwise classify as the calmest regime on record; a malformed
upstream contract (Stage 2 raises on a dict where a `RegimeOutput` belongs);
model output failing schema validation or covering the wrong symbols; a cache
miss in replay; any proposal breaching a limit. In the demo run that is 335
refusal lines of 748, decomposing exactly as the gates predict: 127 `no_data`, 33
`regime/insufficient_history`, 16 `risk/insufficient_history`, 159
`insufficient_history_for_es`.

**Not caught.**

- *Plausible-but-wrong prices.* A bad tick that is positive, ordered and in-range
  passes everything. Cross-venue corroboration is the real fix; an *n*-sigma jump
  filter that no other symbol shows is the cheap interim.
- *Survivorship.* The universe is fixed at snapshot time. Point-in-time universe
  construction from `exchangeInfo` snapshots is the honest version.
- *Regime noise.* Over 135 decisions BTCUSDT changes regime 61 times (46%),
  ETHUSDT 51, SOLUSDT 56, two thirds of them the trend leg. At a weekly cadence
  consecutive decisions share 15 of 20 observations, so the label should be far
  stickier; it is not, because the trend leg is a sign test on a quantity that
  sits near zero. Hysteresis needs state, and state read from the previous
  decision breaks as-of reproducibility — smoothing would have to be another pure
  function of data ≤ *t*.
- *Overlapping windows.* Rolling volatility observations are heavily
  autocorrelated, so the percentile's effective sample is far smaller than its
  nominal one. It ranks the tape; it is not a calibrated probability.
- *Equal weighting in the ES window.* A day eleven months old counts as much as
  yesterday, so the ES is slow to acknowledge a regime change. Filtered
  historical simulation is the production answer, and is not used here because a
  fitted parameter must be re-estimated point-in-time at every date or the log is
  contaminated — the argument that also keeps Stage 1 on rolling statistics.
  `pipeline/risk.py` covers this, and why the window is 365 not the TradFi 252.
- *Model-side drift.* Not hypothetical — the model originally pinned here was
  retired mid-build. The dated id, never a moving alias, sits in the cache key
  and on every line; `tests/test_sdk_contract.py` catches the client-side half.
- *Prompt injection via data.* Unreachable today, since the payload is numeric
  and built field by field. Reachable the moment any text source enters Stage 3.

## 6. Validating this before client capital depends on it

Replay-and-diff is already one command; make it a CI gate. Grow the adversarial
data suite so every incident class that occurs gets a fixture asserting on the
*reason code*, not merely that something raised. Shadow-run daily against the
live feed for a quarter with no capital attached, reconciling each date against a
re-derivation from the vendor snapshot. Instrument the refusal rate as a
first-class metric — a rate falling to zero is a bug report about the gate, not
good news — and the override rate: rules firing constantly mean the prompt and
the limits disagree, and the prompt should be fixed rather than the limit
widened, while rules that never fire (`R1`, `R6` today) should be reproduced in a
replay harness or dropped, not shipped as unexercised safety. Sweep the
thresholds in `config.py` for sensitivity. And review the boundary itself: the
failure that would hurt most is not a wrong number, it is scope creep in what the
LLM is allowed to decide.

## 7. Cost and latency

One model call per run covering the whole universe — not one per symbol —
averaging 1 638 input and 353 output tokens, about **$0.0034** at Haiku 4.5 list
price. The full backfill was 135 billable calls (52 Fridays had no symbol the
pipeline would stand behind, so no call was made): 221 078 input and 47 719
output tokens, **$0.46**. Wall clock is the API round trip, 1–3 s; the
deterministic stages are milliseconds on ~1 300 rows, and replay never touches
the network.

What I would optimise: nothing. A weekly run costs a third of a cent. The
backfill is embarrassingly parallel but a one-off, so a thread pool buys five
minutes once at the cost of concurrency in an orchestrator whose main virtue is
being a `for` loop. At intraday cadence or hundreds of symbols: batching per call
is already the design, then prompt caching on the static system block (1 638
input tokens of mostly-constant instructions against 353 output), and only then a
smaller model. The expensive part of this system is never the tokens — it is the
verification.

## 8. What the AI tooling got wrong

Eight entries in `NOTES.md`, kept while building. The pattern is that the
dangerous mistakes never looked wrong.

A quantiser violated the invariant its own comment asserted —
`math.floor(0.3/0.05)*0.05` is `0.30000000000000004`, above the 0.30 ceiling it
was enforcing — found by a property test over the grid, not by reading it. A
persistent pull toward softening refusals into defaults (`except
ValidationError: return neutral`; an ES window that shrinks when history is
thin), which reads as robustness and is exactly the failure this brief describes.
Two notions of "a year" in one risk module, `sqrt(365)` beside a 252-return
lookback — invisible to every test, because it fails only the question "why 252
here?". A ranking key that was reproducible but not comparable, which duly picked
a symbol with 15 candles.

Two share a shape worth naming: `temperature=` (removed from the SDK in 1.0.0)
and a retired model id both sat in `_call_live`, the one path the offline suite
cannot execute, and surfaced 52 dates into a live run; and `--check` compared
`llm_source`, so it failed on all 413 decision lines the first time it was used
in earnest, having passed every test — the tests only ever compared a replay
against a replay. In both, the comparison that mattered in production was not the
one being tested. If a path cannot be *executed* in CI, assert its *contract* in
CI.
