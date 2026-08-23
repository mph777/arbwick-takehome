# Build notes — what the AI tooling got wrong, and how it was caught

Kept while building, not reconstructed afterwards. The writeup summarises this
file; the detail lives here.

---

## 1. Floating-point quantisation silently broke the risk ceiling

**What was generated.** The tilt quantiser rounded down to the 0.05 grid as
`math.floor(tilt / 0.05) * 0.05`, with a comment asserting the property that
matters: quantisation can never increase risk.

**What was actually true.** `6 * 0.05 == 0.30000000000000004` in binary floating
point. For a `risk_off` stance the ceiling is exactly 0.30, so the function
returned a value strictly *above* the ceiling it had just enforced, and the
override log recorded `before='0.3' after='0.30000000000000004'` — a "clamp" that
loosened the limit.

**How it was caught.** Not by reading it. A parametrised property test over the
stance × tilt grid asserting `0 <= tilt <= ceiling[stance]` and
`final_tilt <= proposed_tilt` failed on three of fifteen cases. The fix is a
`round(..., 10)` after the multiply.

**The general lesson.** The generated code was plausible, commented, and its
comment stated the exact property it violated. Reviewing intent finds nothing
here; asserting the invariant does. Every limit in `constraints.py` now has a
property test rather than an example test.

---

## 2. A test fixture that quietly tested nothing

**What was generated.** End-to-end tests that ran the orchestrator "at the end of
the sample", written as `START + timedelta(days=699)`.

**What was actually true.** That date is a Wednesday. The cache fixture only
primes Fridays, so the run raised `CacheMiss` — which, this time, was loud.

**Why it is worth recording anyway.** The failure was visible only because the
cache fails closed. Had `replay` fallen back to a live call, or returned a
nearest-match, these three tests would have passed while asserting on a date the
demo run never covers. The bug was in the test, but the reason it surfaced was a
design decision in the code under test.

---

## 3. Tendency to soften refusals into defaults

**What kept happening.** Across several drafts of Stage 3, the generated
error-handling path ended in a fallback: `except ValidationError: return
neutral`, or an ES window that shortened itself when history was thin. Both read
as robustness. Both are exactly the failure this brief describes — a plausible
answer papered over a missing input.

**How it was handled.** Refusal is a first-class return type
(`SkillRefusal` → a `refusal` log line), never an exception swallowed into a
default, and there is a test that asserts a Stage 3 failure produces refusals for
every symbol rather than a neutral stance. `MIN_CANDLES_ES` is a hard floor with
its own test asserting the window is never shortened.

---

## 4. Point-in-time filtering wanted to spread out

**What kept happening.** Draft code applied the as-of cut inside each stage —
`df[df.date <= as_of]` at the top of the regime function, again in risk, again in
the quality gate. Each instance was correct in isolation.

**Why it was consolidated.** Four copies of an invariant is four places to get it
wrong later, and a reviewer has to check all four to believe the pipeline. The
cut now happens once, in `loader.load_as_of`, and no stage ever receives an
unfiltered frame. The leakage tests append violent future candles and assert the
as-of-t output is bit-identical — that is the check that would have caught a
missed copy, and it is cheap enough to keep forever.

---

## 5. Two different notions of "a year" in one risk module

**What was generated.** Volatility annualised with `sqrt(365)` — correct, Binance
spot trades every calendar day — and, forty lines below, an ES lookback of 252
returns. Both figures are individually conventional. Together they say the year
has 365 days and also 252 days.

**Why it survived review for a while.** Nothing catches it. Every test passes,
`verify.py` is clean, the ES numbers look entirely plausible, and 252 is such a
standard constant in risk code that it reads as correct rather than as an
assumption. It only fails the question "why 252 here?", which no test asks.

**The fix.** `ES_WINDOW = 365`, so 366 closes are required and the 5% tail is 18
observations rather than 12. The cost is visible and correct: every symbol now
refuses for its first year rather than its first 252 days, which pushes the
first decision in the sample from September 2023 to January 2024.

**The general lesson.** The mistakes that survive are not the ones that look
wrong; they are defaults imported wholesale from an adjacent domain. A TradFi
session count in a market with no sessions is invisible until someone asks what
the constant means. Worth a pass over every bare number in `config.py` asking the
same question.

## 6. A ranking key that was reproducible but not comparable

**What happened.** Late-symbol discovery originally ranked candidates by 24h
quote volume from `/ticker/24hr` — a rolling window measured at request time, so
re-running the fetch could select a different symbol and produce a different
snapshot. Replaced with the median daily quote volume over each symbol's own
history inside the window, which fixed reproducibility.

**What that missed.** Reproducible, but not comparable across candidates. A
symbol with fifteen days in the window is ranked on fifteen recent, active days;
one with four hundred is ranked on a distribution including its quiet ones. The
key therefore selected whichever symbol had listed most recently — AEROUSDT, with
15 candles, which can never satisfy any history gate and contributes three
refusal lines to the log and nothing else.

**How it was caught.** By reading the output of the first real fetch and asking
whether the chosen symbol could actually demonstrate anything. No test could have
found this: the code did exactly what it said, and what it said was wrong.

**The fix.** Rank on a fixed trailing slice of the window, identical for every
candidate, and require the winner to reach the history gate so the log shows the
gate opening rather than only refusing.

## 7. Code written against an API that had moved on

Two defects with one root cause, both found by the first live run rather than by
the test suite.

**`temperature=` in `messages.create()`.** Removed from the anthropic SDK in
1.0.0. The call raised `TypeError: unexpected keyword argument 'temperature'` -
52 dates into a 187-date weekly run, on the first date that actually reached
Stage 3, because every earlier date refused before the model was called. Nothing
was wasted, but only by luck of ordering.

**A retired model id.** `claude-3-5-haiku-20241022` was pinned from memory and is
no longer in the model catalogue; the run would have failed on the next line.

**Why the suite did not catch either.** `_call_live` is the single code path the
offline tests cannot execute - it needs a key and a network - so it was also the
only place where a plausible-looking call could survive unexecuted. Everything
around it was tested; the call itself was not.

**The fix.** `tests/test_sdk_contract.py` introspects the *installed* SDK instead
of calling it: it parses the keyword arguments out of `_call_live` and asserts
every one exists in `Messages.create`'s signature, asserts the arguments the
design depends on (`tools`, `tool_choice`, `system`) are still available, and
asserts the `usage` fields the cost report reads still exist. No key, no network,
no cost, and it would have failed before the run started.

Two design consequences, both kept:

* Sampling parameters are gone from the cache key entirely. Their availability
  varies by SDK version, and a key that depends on the SDK version produces a
  spurious cache miss on a reviewer's machine. Reproducibility was never coming
  from a temperature setting - it comes from the committed cache.
* The model id stays a dated snapshot rather than the `claude-haiku-4-5` alias.
  An alias moves silently; the pinned id is in the cache key and on every log
  line, so the next deprecation shows up as a visible discontinuity instead of a
  quiet change in what produced the decisions.

**The general lesson.** This is the same failure as the 252-day year: code
generated against a remembered version of the world, in the one spot no test
executes. The rule that falls out of it - if a code path cannot be executed in
CI, assert its *contract* in CI - is cheap and would have caught both.

## Add as you go

- [ ] anything the live fetch surfaces that the offline build could not
