"""Plain Python orchestrator.

The DAG is linear and fixed: quality -> regime -> risk -> allocation -> clamp.
There is no planning to do, no tool selection, no branching an agent could
usefully reason about, and exactly one model call in the whole chain. An agent
framework here would add a scheduler, a retry policy and a serialisation format
on top of five function calls, and would make the point-in-time guarantee harder
to see rather than easier. The framework earns its place when control flow is
genuinely dynamic; this one is a for-loop.

Refusal is per symbol. A date is not thrown away because the newest listing in
the universe has 40 days of history - that symbol is refused, with a reason, and
the others still get a decision. Refused symbols are removed from the payload
entirely, so the model is never shown a number the pipeline would not stand
behind.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import config as cfg
from pipeline import allocation, constraints, quality, regime as regime_stage, risk as risk_stage
from pipeline.llm_cache import LLMCache, canonical, sha256
from pipeline.loader import as_of_cutoff_ms, load_as_of, snapshot_sha256, universe
from pipeline.models import (
    DecisionLogEntry,
    Refusal,
    RunProvenance,
    SkillRefusal,
)


def git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cfg.REPO_ROOT,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


def params_sha256() -> str:
    """Hash of every parameter that can change a decision."""
    return sha256(canonical({
        "regime_method": cfg.REGIME_METHOD_VERSION,
        "trend_window": cfg.TREND_WINDOW,
        "vol_window_regime": cfg.VOL_WINDOW_REGIME,
        "vol_pctl_split": cfg.VOL_PCTL_SPLIT,
        "min_candles_regime": cfg.MIN_CANDLES_REGIME,
        "min_vol_observations": cfg.MIN_VOL_OBSERVATIONS,
        "annualisation_days": cfg.ANNUALISATION_DAYS,
        "risk_method": cfg.RISK_METHOD_VERSION,
        "vol_window_risk": cfg.VOL_WINDOW_RISK,
        "es_window": cfg.ES_WINDOW,
        "es_confidence": cfg.ES_CONFIDENCE,
        "min_candles_risk": cfg.MIN_CANDLES_RISK,
        "min_candles_es": cfg.MIN_CANDLES_ES,
        "max_gap_days": cfg.MAX_CONSECUTIVE_GAP_DAYS,
        "max_staleness_days": cfg.MAX_STALENESS_DAYS,
        "max_zero_volume_tail": cfg.MAX_ZERO_VOLUME_TAIL,
        "tilt_ceilings": cfg.TILT_CEILING_BY_STANCE,
        "tilt_quantum": cfg.TILT_QUANTUM,
        "c_extreme_vol_pctl": cfg.C_EXTREME_VOL_PCTL,
        "c_deep_drawdown": cfg.C_DEEP_DRAWDOWN,
        "c_deep_drawdown_tilt_cap": cfg.C_DEEP_DRAWDOWN_TILT_CAP,
        "c_severe_es": cfg.C_SEVERE_ES,
        "c_severe_es_tilt_cap": cfg.C_SEVERE_ES_TILT_CAP,
    }))


def run_as_of(
    as_of: date,
    cache: LLMCache,
    snapshot_dir: Path | None = None,
    symbols: list[str] | None = None,
) -> list[DecisionLogEntry]:
    cutoff_ms = as_of_cutoff_ms(as_of)
    symbols = symbols or universe(snapshot_dir)

    prov = RunProvenance(
        schema_version=cfg.LOG_SCHEMA_VERSION,
        as_of=as_of,
        as_of_cutoff_ms=cutoff_ms,
        snapshot_sha256=snapshot_sha256(snapshot_dir),
        git_commit=git_commit(),
        model_id=cfg.LLM_MODEL,
        prompt_sha256=sha256(allocation.system_prompt()),
        params_sha256=params_sha256(),
    )

    entries: list[DecisionLogEntry] = []
    reads = []

    for symbol in symbols:
        df = load_as_of(symbol, cutoff_ms, snapshot_dir)

        report = quality.check(symbol, df, as_of)
        if not report.ok:
            entries.append(_refusal(symbol, prov, quality.STAGE,
                                    "+".join(report.reason_codes),
                                    "; ".join(report.issues)))
            continue

        try:
            reg = regime_stage.classify(symbol, df, as_of)
            rsk = risk_stage.assess(symbol, df, reg)
        except SkillRefusal as exc:
            entries.append(_refusal(symbol, prov, exc.stage, exc.reason_code, exc.detail))
            continue

        reads.append((reg, rsk))

    if reads:
        try:
            llm_response, source, key = allocation.decide(as_of, reads, cache)
        except SkillRefusal as exc:
            # Stage 3 failed for the whole date: every symbol that reached it is
            # refused with the same reason, and no stance is invented.
            for reg, _ in reads:
                entries.append(_refusal(reg.symbol, prov, exc.stage,
                                        exc.reason_code, exc.detail))
        else:
            by_symbol = {d.symbol: d for d in llm_response.decisions}
            for reg, rsk in reads:
                final = constraints.apply(reg, rsk, by_symbol[reg.symbol])
                entries.append(DecisionLogEntry(
                    record_type="decision", symbol=reg.symbol, provenance=prov,
                    regime=reg, risk=rsk, decision=final,
                    llm_source=source, llm_cache_key=key,
                ))

    entries.sort(key=lambda e: e.symbol)
    return entries


def _refusal(symbol: str, prov: RunProvenance, stage: str,
             code: str, detail: str) -> DecisionLogEntry:
    return DecisionLogEntry(
        record_type="refusal", symbol=symbol, provenance=prov,
        refusal=Refusal(stage=stage, reason_code=code, detail=detail),
    )


def fridays(start: date, end: date) -> list[date]:
    d = start + timedelta(days=(4 - start.weekday()) % 7)
    out = []
    while d <= end:
        out.append(d)
        d += timedelta(days=7)
    return out


def serialise(entry: DecisionLogEntry) -> str:
    return json.dumps(entry.model_dump(mode="json", exclude_none=True),
                      sort_keys=True, separators=(",", ":"))


def write_log(entries: list[DecisionLogEntry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for e in entries:
            fh.write(serialise(e) + "\n")


# Fields that describe THIS EXECUTION rather than the decision it produced, and
# are therefore excluded from the reproducibility comparison. Everything else -
# regime, risk, stance, tilt, rationale, every constraint applied, the snapshot
# hash, the prompt and parameter hashes, the as-of cutoff, the cache key - must
# match byte for byte.
#
#   git_commit  the code may be committed after the log was written
#   llm_source  "live" on the run that recorded the cache, "cache" on every
#               replay of it. The answer is identical either way; only the route
#               to it differs, and the cache key (which IS compared) identifies
#               the request and response exactly.
VOLATILE_FIELDS = {"llm_source"}
VOLATILE_PROVENANCE_FIELDS = {"git_commit"}


def normalise(line: str) -> str:
    row = json.loads(line)
    row = {k: v for k, v in row.items() if k not in VOLATILE_FIELDS}
    row["provenance"] = {k: v for k, v in row["provenance"].items()
                         if k not in VOLATILE_PROVENANCE_FIELDS}
    return json.dumps(row, sort_keys=True, separators=(",", ":"))


def report_reproducibility(path: Path, entries: list[DecisionLogEntry]) -> None:
    """Diff a regenerated log against the committed one, and say where."""
    committed = [normalise(l) for l in path.read_text().splitlines() if l.strip()]
    regenerated = [normalise(serialise(e)) for e in entries]

    if committed == regenerated:
        print(f"\nreproducible: {len(regenerated)} lines identical to {path}")
        print(f"  (excluded from the comparison: "
              f"{', '.join(sorted(VOLATILE_FIELDS | VOLATILE_PROVENANCE_FIELDS))})")
        return

    print(f"\nNOT reproducible: regenerated log differs from {path}", file=sys.stderr)
    if len(committed) != len(regenerated):
        print(f"  line count: committed {len(committed)}, regenerated "
              f"{len(regenerated)}", file=sys.stderr)

    shown = 0
    for i, (a, b) in enumerate(zip(committed, regenerated), 1):
        if a == b:
            continue
        shown += 1
        if shown > 3:
            break
        ja, jb = json.loads(a), json.loads(b)
        keys = sorted(set(ja) | set(jb))
        print(f"\n  line {i} differs:", file=sys.stderr)
        for k in keys:
            if ja.get(k) != jb.get(k):
                print(f"    {k}:\n      committed   {json.dumps(ja.get(k))[:300]}"
                      f"\n      regenerated {json.dumps(jb.get(k))[:300]}", file=sys.stderr)
    total = sum(1 for a, b in zip(committed, regenerated) if a != b)
    print(f"\n  {total} of {min(len(committed), len(regenerated))} compared lines "
          f"differ.", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="ArbWick regime-to-allocation pipeline")
    ap.add_argument("--as-of", type=date.fromisoformat,
                    help="run a single as-of date (YYYY-MM-DD)")
    ap.add_argument("--weekly", action="store_true",
                    help="run as-of every Friday in the sample window")
    ap.add_argument("--mode", choices=("replay", "live", "refresh"), default="replay",
                    help="replay (default, offline, no key needed) | live | refresh")
    ap.add_argument("--out", type=Path, default=cfg.DECISION_LOG_FILE)
    ap.add_argument("--check", action="store_true",
                    help="regenerate the log and diff it against the committed one")
    args = ap.parse_args()

    if not args.as_of and not args.weekly:
        ap.error("pass --as-of DATE or --weekly")

    cache = LLMCache(mode=args.mode)
    dates = [args.as_of] if args.as_of else fridays(cfg.WINDOW_START, cfg.WINDOW_END)

    entries: list[DecisionLogEntry] = []
    for i, d in enumerate(dates, 1):
        got = run_as_of(d, cache)
        entries.extend(got)
        n_ref = sum(1 for e in got if e.record_type == "refusal")
        print(f"[{i:>3}/{len(dates)}] {d}  decisions={len(got) - n_ref}  refusals={n_ref}",
              flush=True)

    if args.check:
        report_reproducibility(args.out, entries)
        return

    write_log(entries, args.out)
    n_ref = sum(1 for e in entries if e.record_type == "refusal")
    n_over = sum(1 for e in entries
                 if e.decision and e.decision.constraints_applied)
    print(f"\nwrote {len(entries)} lines to {args.out}")
    print(f"  decisions {len(entries) - n_ref}, refusals {n_ref}, "
          f"lines with a code override {n_over}")


if __name__ == "__main__":
    main()
