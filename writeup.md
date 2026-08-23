# Regime → risk → allocation: design notes

> Every figure below comes from `python -m tools.report` run against the
> committed decision log. None is typed by hand.

## 1. Where the code/LLM boundary sits, and why

The boundary is drawn at **verifiability**. Anything with a defensible mechanical
answer is code; the LLM gets the one job left over, which is weighing several
partially-conflicting readings into a single posture and saying why.

| | decided by | reason |
|---|---|---|
| Regime label | code | A rolling statistic is reproducible and auditable. An LLM asked to label a regime from the same numbers would be a slower, non-deterministic implementation of a threshold. |
| Drawdown, vol percentile, ES95 | code | Arithmetic. |
| Whether a date is answerable at all | code | The refusal decision must be the one thing in the chain that cannot be talked out of. |
| Risk limits | code | Below. |
| Stance and sizing tilt | **LLM** | Three measures that routinely disagree — a shallow drawdown with a 95th-percentile vol, a deep drawdown with a calm tape. Encoding the trade-off as a lookup table means inventing a weighting nobody can defend; this is genuinely a judgement call over a small, fully-specified input. |
| Rationale | **LLM** | The auditable artefact. It is what an auditor reads to understand why. |

**Stage 3 receives nothing but Stage 1 and Stage 2 output as JSON.** No prices, no
history, no dates beyond the as-of label. It cannot compute anything itself, so
it cannot contradict the deterministic stages, and it has no data with which to
hallucinate a market narrative.

### What the LLM is explicitly forbidden to decide, and how that is enforced

1. **The regime and every number.** Structural: they are computed upstream and
   passed in. The model has no tool with which to change them.
2. **Whether to refuse.** Refusal is decided by `quality.py`, Stage 1 and Stage 2
   before the model is called. A refused symbol is *removed from the payload*, so
   the model is never shown a figure the pipeline would not stand behind — and
   never gets the chance to reason around a missing one.
3. **The risk limits.** Six rules in `constraints.py`, each a pure function of
   `(regime, risk, proposal)` with a stable id:
   - `R1` `bear_high_vol` with 30d vol percentile > 0.90 → `risk_on` unavailable
   - `R2` drawdown ≤ −35% → `risk_on` unavailable
   - `R3` tilt ceiling by final stance (risk_off 0.30 / neutral 0.60 / risk_on 1.00)
   - `R4` drawdown ≤ −35% → tilt ≤ 0.50
   - `R5` 1d ES95 ≤ −8% → tilt ≤ 0.40
   - `R6` tilt rounded **down** to the 0.05 grid
   The clamp never reads the rationale, so a persuasive argument cannot buy an
   exemption — there is a test that asserts exactly that. Overrides are not
   silent: the log keeps `llm_stance` and `llm_sizing_tilt` alongside the final
   values and lists every rule that fired.
   In the demo run 86 of 413 decisions were modified by a rule, 31 of them in
   the stance rather than only the tilt. The model proposed `risk_on` 136 times
   and was allowed it 105 times. Which rules did the work is itself a finding:
   `R5` (severe ES) fired 66 times, `R4` (deep-drawdown tilt cap) 36, `R2`
   (deep-drawdown stance veto) 31, `R3` (stance ceiling) 25 — and **`R1` never
   fired at all**, nor did `R6`. `R1` is the rule this design started from, and
   on real data the combination it guards (a `bear_high_vol` tape above the 90th
   volatility percentile *and* a model asking for `risk_on`) never occurred; the
   drawdown and tail-loss rules did the actual restraining. `R6` never fired
   because the model emitted grid-aligned tilts unprompted. Both are reported
   rather than quietly kept: a rule that has never fired is a rule that has never
   been tested outside its unit test, and I would not present either as evidence
   the clamp layer works — the other four are that evidence.
4. **Its own output shape.** Structure is forced through a tool schema and
   re-validated against a Pydantic model, including that the set of symbols
   returned equals the set sent. One reprompt carrying the validation error, then
   the date is refused. There is no fallback to `neutral`.

### Orchestration: plain Python

The DAG is linear and fixed, with one model call in it. An agent framework would
add a scheduler, a retry policy and a serialisation layer on top of five function
calls, and would make the point-in-time guarantee harder to see rather than
easier. The rule of thumb: a framework earns its place when control flow is
genuinely dynamic. Here it is a `for` loop, and the architecture's job is to make
the leakage boundary obvious.

## 2. Point-in-time construction

A decision dated *t* is stamped **00:00:00.000 UTC of t+1** and may use every
daily candle closed by then — so candle *t* is included and nothing later exists.
The boundary is expressed as a strict `<` against the next midnight, which avoids
depending on whether the exchange reports `close_time` to the millisecond.

The cut happens **once**, in `loader.load_as_of`. No stage receives an unfiltered
frame, so leakage is structurally impossible rather than a property each stage
must independently preserve. Two test families defend this:

- **Truncation equivalence** — as-of *t* on the full snapshot equals as-of *t* on
  a snapshot physically truncated at *t*.
- **Future invariance** — appending 120 synthetic candles after *t* at 10× and
  0.1× the last close, at 20% daily vol, must leave the as-of-*t* output
  bit-identical. This is the test that catches a rolling window computed before
  the cut, a percentile ranked against the whole file, or a `.max()` on an
  unfiltered frame.

## 3. Reproducibility

Same snapshot + same code + same cache → byte-identical log. Two things make
that true across machines:

- **The model call is content-addressed and committed.** Every request is keyed
  by a hash of model id, token limit, system prompt, tool schema and the exact
  payload — deliberately not of any sampling parameter, whose availability varies
  by SDK version and would make the key produce spurious misses on a reviewer's
  machine; the response is committed under `llm_cache/`. `replay` is the
  default mode and never touches the network, so the log reproduces offline
  without a key. This is a *record*, not a hard-coded answer: the key is derived
  from the input, so changing a Stage 2 statistic changes the key and replay then
  fails loudly with a cache miss rather than answering a question that was never
  asked. Payload floats are rounded to six decimals so a last-bit difference in a
  volatility estimate cannot silently invalidate the cache.
- **Every line is self-describing.** Snapshot hash, git commit, model id, prompt
  hash, parameter hash and the as-of cutoff in milliseconds ride on each line, so
  an auditor can tell whether two lines were produced by the same pipeline
  without trusting the runner.

`python -m pipeline.orchestrator --weekly --check` regenerates and diffs.

## 4. Data verification

`data/fetch.py` writes each kline field verbatim as the string the exchange
returned — no float round-trip between API and disk — so the file hash is a
meaningful identity for "what Binance said" and re-fetching is byte-stable. It
pages `/api/v3/klines` at 1000 candles per request, de-duplicating on `open_time`
because `startTime` is inclusive and a naive cursor overlaps, and it drops any
candle that had not closed at fetch time.

**The late-listed symbol was chosen from the data, not from memory — and the
choice is reproducible.** The script probes the first available daily candle of
every USDT spot pair (`startTime=0, limit=1`) — 482 pairs, of which 134 listed
after 2025-06-01 — then fetches each candidate's window history and applies
three criteria:

1. unbroken daily history from listing to the window end;
2. at least `MIN_CANDLES_ES` candles by the window end;
3. highest median daily quote volume over the **last 30 days of the window**,
   tie-broken on symbol name.

Criterion 2 is the one worth explaining. A symbol listed weeks before the window
closes satisfies the brief and contributes nothing: it can only ever refuse.
Requiring the late symbol to reach the history gate means the decision log shows
the gate *opening* — refusals with a reason, then decisions at the exact date the
evidence becomes sufficient — which is the behaviour worth demonstrating.

Criterion 3 went through two wrong versions before this one, and both are
instructive. The obvious key, 24h quote volume from `/ticker/24hr`, is a rolling
window measured at request time: two similar candidates swap places between runs
and the fetch produces a different snapshot each time. The replacement — median
volume over each symbol's own in-window history — is reproducible but not
*comparable*: a symbol with fifteen days in the window is ranked on fifteen
recent active days while one with four hundred is ranked on a distribution that
includes its quiet ones, so the key systematically selects the most recent
listing, i.e. exactly the symbol least able to show anything. A fixed trailing
slice, identical for every candidate, is both.

The residual source of drift is stated rather than hidden — a symbol delisted
between runs leaves `exchangeInfo` and so the candidate set — and
`LATE_SYMBOL_PIN` in `config.py` closes it: once chosen, the symbol is pinned,
discovery must still support it, and discovery becomes the justification for the
choice rather than a live dependency on it.

The full scored candidate table is committed in
`data/snapshot/late_symbol_selection.json`. Nine of the 134 candidates satisfy
criteria 1 and 2. Selected: **HOMEUSDT**, first candle 2025-06-12, 415 candles at
the window end. Its 366th candle falls on 2026-06-12, which is itself a Friday,
so the log carries 179 refusals for that symbol and then 8 decisions, with the
transition landing exactly on a decision date rather than somewhere between two.

`data/verify.py` splits findings in two, which is the part that matters:

- **Hard** (exits non-zero, snapshot unusable): manifest sha256 mismatch,
  duplicate or non-monotonic `open_time`, `close_time − open_time ≠ 86 399 999 ms`,
  non-positive prices, `high < low`, high below open/close, low above open/close,
  negative volume, taker-buy volume exceeding total volume, NaN in OHLCV, data
  outside the mandated window, and a late symbol whose first candle contradicts
  the listing probe.
- **Soft** (reported, never patched): missing calendar days, gap runs,
  zero-volume days, flat-range days, and date-set differences between symbols.

The distinction is deliberate. A soft finding is a real property of exchange
history, and patching it at fetch time — forward-filling a missing day, say —
would hide from the pipeline exactly the condition it is supposed to refuse on.
So irregularities survive into the snapshot and are handled at run time by the
quality gate, which refuses a symbol when the trailing window it actually
consumes contains a gap longer than 5 days, a stale tail, a NaN, a duplicate
timestamp, or 5 consecutive zero-volume days.

**What the fetch found:** nothing irregular. All three core symbols return 1308
candles spanning 2023-01-01 to 2026-07-31 with no missing calendar days, no gaps,
no zero-volume days and no flat-range days; their date sets are identical, so
there is no alignment difference to handle. HOMEUSDT returns 415 candles from its
listing date with the same cleanliness, and its first snapshot candle matches the
independent listing probe. Every hard check passes.

That is a cleaner result than expected — the brief warns that real exchange
history contains irregularities, and on this universe and window it does not. The
checks are not therefore decorative: they are the reason the claim "no
irregularities" can be made at all rather than assumed, and the run-time gate
still refuses on the same conditions should a future snapshot contain them. The
broken-data tests in `tests/test_failloud.py` exercise every one of those paths
against synthetic snapshots precisely because the real one does not.

Two run-time consequences worth stating:

- Symbols are **not** date-aligned into one frame. Each is loaded and cut
  independently, so one symbol's hole cannot shift another's window.
- Every symbol refuses for its first 365 days because the ES window is never
  shortened (below). In the demo run that is 335 refusal lines of 748, and it is
  the correct answer rather than a gap in coverage. They decompose exactly as the
  gates predict: 127 `data_quality/no_data` (HOMEUSDT before it existed), 33
  `regime/insufficient_history` (under 60 candles), 16 `risk/insufficient_history`
  (under 90), and 159 `risk/insufficient_history_for_es` (under 366). Each symbol
  passes through the gates in order and the log names which one is holding.

## 5. Failure modes

**Caught now.** Insufficient history (three distinct floors, each with its own
reason code); gaps inside the consumed window; stale feed; duplicate or
non-monotonic timestamps; NaN and non-positive prices; a frozen feed, which
produces zero realised volatility and would otherwise classify as the calmest
regime on record; a malformed upstream contract (Stage 2 raises on a dict where a
`RegimeOutput` belongs, rather than coercing); model output that fails schema
validation; a model response covering the wrong set of symbols; a cache miss in
replay mode; and any proposal that breaches a risk limit.

**Not caught, and what I would do about it.**

- **Plausible-but-wrong prices.** A bad tick that is positive, ordered and
  in-range passes every check. Cross-venue corroboration on close is the real
  fix; a cheaper interim is a jump filter that refuses on an *n*-sigma close-to-close
  move that no other symbol shows.
- **Survivorship in the universe.** The universe is fixed at snapshot time, so a
  symbol delisted mid-sample would simply be absent from history rather than
  present-then-gone. Point-in-time universe construction from `exchangeInfo`
  snapshots is the honest version.
- **Regime noise.** The trend leg flips whenever the 20d return crosses zero, so
  the label is unstable: over 135 decisions BTCUSDT changes regime 61 times
  (46%), ETHUSDT 51 (38%) and SOLUSDT 56 (42%), and roughly two thirds of those
  are the trend leg flipping rather than the volatility leg. A weekly cadence on
  a 20-day return means consecutive decisions share 15 of 20 observations, so the
  label should be far stickier than that; it is not, because the trend leg is a
  sign test on a quantity that spends much of its time near zero. This is a known
  property, not a bug. Hysteresis would fix it but requires state, and state read from the
  previous decision would break "re-running as-of an earlier date reproduces that
  entry". Smoothing would have to be another pure function of data ≤ *t*.
- **Overlapping windows.** Rolling 20d volatility observations are heavily
  autocorrelated, so the percentile's effective sample is much smaller than its
  nominal one. It ranks the tape honestly; it is not a calibrated probability.
- **Equal weighting inside the ES window.** This is the real limitation, not the
  window's length: a day eleven months old counts as much as yesterday, so in a
  market where volatility clusters the ES is slow to acknowledge a regime change,
  and a 365-day tail contains the last year's worst days and nothing older. Age-
  weighted or filtered historical simulation — standardise returns by a
  GARCH/EWMA volatility estimate, take the tail of the residuals, rescale by
  today's volatility — is the production answer. It is not used here because a
  fitted parameter has to be re-estimated point-in-time at every as-of date or
  the log is contaminated, which is the same argument that keeps Stage 1 on
  rolling statistics; the inertia is partly covered by the 30d volatility
  percentile sitting beside it.
- **Model-side drift.** A model deprecation or a silent server-side change alters
  Stage 3 without any input changing — this is not hypothetical: the model
  originally pinned here was retired during the build. The cache pins the
  historical log, the dated model id (never a moving alias) sits in the cache key
  and on every line so the discontinuity is visible, and the SDK-contract tests
  catch the client-side half of it. Nothing prevents the server-side half.
- **Prompt injection via data.** Not reachable today — the payload is numeric and
  built field by field — but it becomes reachable the moment any text source
  (news, filings) enters Stage 3.

## 6. Validating this before client capital depends on it

1. **Replay the whole sample and diff against the committed log on the reviewer's
   machine.** Already a command; make it a CI gate.
2. **Adversarial data suite in CI.** The current broken-data tests are the seed:
   every incident class that ever occurs gets a fixture, and the assertion is on
   the *reason code*, not merely that something raised.
3. **Shadow-run before it decides anything.** Run daily against the live feed for
   a quarter with no capital attached, and reconcile every date against a
   re-derivation from the vendor snapshot.
4. **Instrument the refusal rate as a first-class metric.** A refusal rate that
   drops to zero is a bug report about the gate, not good news.
5. **Instrument the override rate.** Rules that fire constantly mean the prompt
   and the limits disagree, and the prompt should be fixed rather than the limit
   widened. Rules that never fire are untested outside their unit test — `R1` and
   `R6` are in that position today (§1), and before capital depended on this I
   would either construct the conditions in a replay harness or drop them, not
   ship them as unexercised safety.
6. **Sensitivity sweep on the thresholds.** Every constant in `config.py` is a
   choice; the ones the decision log is materially sensitive to need a stated
   rationale, and the ones it is not can be simplified away.
7. **A second pair of eyes on the boundary itself.** The failure that would hurt
   most is not a wrong number — it is scope creep in what the LLM is allowed to
   decide. That is a code-review rule, not a test.

## 7. Cost and latency

Per weekly run: **one** model call covering the whole universe — not one per
symbol — averaging 1 638 input and 353 output tokens, about **$0.0034** at Haiku
4.5 list price. The full-sample backfill was 135 billable calls (the other 52
Fridays had no symbol the pipeline would stand behind, so no call was made):
221 078 input and 47 719 output tokens, **$0.46** in total.

Wall clock is dominated by the API round trip at roughly 1–3 s per call; the
deterministic stages are a few milliseconds each on ~1 300 rows, and an offline
replay of the whole sample runs from the committed cache without touching the
network.

What I would optimise, in order: nothing. A production weekly run is one call
costing a third of a cent, where latency is irrelevant. The backfill took a few
minutes because dates run sequentially, and it is embarrassingly parallel — but
it is also a one-off, after which every run replays from cache, so a thread pool
would buy five minutes once at the cost of concurrency in an orchestrator whose
main virtue is that it is a `for` loop.

If the cadence went intraday or the universe to hundreds of symbols: batching
symbols per call is already the design; next would be prompt caching on the
static system block, which is 1 638 input tokens of mostly-constant instructions
against 353 output; and only then a smaller model. The expensive part of this
system is never the tokens — it is the verification.

## 8. What the AI tooling got wrong

Full detail in `NOTES.md`; the ones that mattered:

1. **A floating-point quantiser that violated the invariant its own comment
   asserted.** `math.floor(t/0.05)*0.05` returns `0.30000000000000004` for
   `t=0.3`, i.e. strictly above the 0.30 `risk_off` ceiling — a clamp that
   loosened the limit it was enforcing. Caught by a property test over the
   stance × tilt grid, not by reading the code. Reviewing intent finds nothing
   here; asserting the invariant does.
2. **A persistent pull toward softening refusals into defaults** — `except
   ValidationError: return neutral`, an ES window that shrinks when history is
   thin. Both read as robustness and both are the exact failure this brief is
   about. Refusal is now a first-class return type with a test asserting a Stage 3
   failure yields refusals rather than a neutral stance.
3. **Point-in-time filtering that wanted to spread across every stage.** Each
   copy was individually correct; four copies is four places to break later.
   Consolidated into the loader, with future-invariance tests as the standing
   check.
4. **A test fixture asserting on a date the run never covered** (a "Friday" that
   was a Wednesday). It surfaced only because the cache fails closed on a miss —
   a reminder that a fallback would have turned a loud test bug into a silent one.
5. **Two different notions of "a year" in one risk module** — `sqrt(365)`
   annualisation beside a 252-return ES lookback. Both constants are individually
   conventional; together they are incoherent, and nothing in the test suite,
   the verifier or the output looks wrong. It fails only the question "why 252
   here?", which no test asks. The same class of error produced a late-symbol
   ranking key that was reproducible but not comparable across candidates, and
   duly selected a symbol with 15 candles that could never clear any gate. Both
   were caught by reading output and asking what a number meant, not by a test.
6. **Code written against an API that had moved on** — `temperature=`, removed
   from the anthropic SDK in 1.0.0, and a model id that had been retired. Both
   sat in `_call_live`, the one code path the offline suite cannot execute, and
   both surfaced 52 dates into a live run. The fix generalises the lesson: if a
   path cannot be *executed* in CI, assert its *contract* in CI —
   `tests/test_sdk_contract.py` parses the keyword arguments out of the call site
   and checks them against the installed SDK's signature, for free and offline.
