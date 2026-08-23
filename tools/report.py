"""Summarise a committed decision log.

Produces the numbers the writeup quotes, so no figure in the writeup is typed by
hand. Run after the weekly demo run:

    python -m tools.report
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else cfg.DECISION_LOG_FILE
    rows = load(path)
    if not rows:
        raise SystemExit(f"{path} is empty")

    dates = sorted({r["provenance"]["as_of"] for r in rows})
    decisions = [r for r in rows if r["record_type"] == "decision"]
    refusals = [r for r in rows if r["record_type"] == "refusal"]

    print(f"log            {path}")
    print(f"schema         {rows[0]['provenance']['schema_version']}")
    print(f"snapshot       {rows[0]['provenance']['snapshot_sha256'][:16]}")
    print(f"model          {rows[0]['provenance']['model_id']}")
    print(f"prompt sha     {rows[0]['provenance']['prompt_sha256'][:16]}")
    print(f"params sha     {rows[0]['provenance']['params_sha256'][:16]}")
    print(f"as-of dates    {len(dates)}  ({dates[0]} .. {dates[-1]})")
    print(f"lines          {len(rows)}  decisions={len(decisions)} refusals={len(refusals)}")

    print("\n-- refusals by reason ---------------------------------------")
    for (stage, code), n in Counter(
            (r["refusal"]["stage"], r["refusal"]["reason_code"]) for r in refusals
    ).most_common():
        print(f"  {n:>5}  {stage}/{code}")

    print("\n-- refusals by symbol ---------------------------------------")
    per_symbol = defaultdict(lambda: [0, 0])
    for r in rows:
        per_symbol[r["symbol"]][0 if r["record_type"] == "decision" else 1] += 1
    for s, (dec, ref) in sorted(per_symbol.items()):
        first_dec = min((r["provenance"]["as_of"] for r in decisions if r["symbol"] == s),
                        default="-")
        print(f"  {s:<12} decisions={dec:>4} refusals={ref:>4}  first decision {first_dec}")

    print("\n-- regime distribution (decided dates only) ------------------")
    for regime, n in Counter(r["regime"]["regime"] for r in decisions).most_common():
        print(f"  {n:>5}  {regime}")

    print("\n-- regime flips per symbol ----------------------------------")
    for s in sorted(per_symbol):
        seq = [r["regime"]["regime"] for r in
               sorted((d for d in decisions if d["symbol"] == s),
                      key=lambda r: r["provenance"]["as_of"])]
        flips = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
        trend_flips = sum(1 for a, b in zip(seq, seq[1:]) if a.split("_")[0] != b.split("_")[0])
        if seq:
            print(f"  {s:<12} {flips:>3} regime changes over {len(seq)} decisions "
                  f"({flips / max(1, len(seq) - 1):.0%}), of which {trend_flips} are trend flips")

    print("\n-- stances ---------------------------------------------------")
    print("  final    ", dict(Counter(r["decision"]["stance"] for r in decisions)))
    print("  proposed ", dict(Counter(r["decision"]["llm_stance"] for r in decisions)))

    print("\n-- code overrides of the model ------------------------------")
    overridden = [r for r in decisions if r["decision"].get("constraints_applied")]
    print(f"  {len(overridden)} of {len(decisions)} decisions had at least one rule applied")
    for rule, n in Counter(
            c["rule_id"] for r in overridden for c in r["decision"]["constraints_applied"]
    ).most_common():
        print(f"  {n:>5}  {rule}")
    stance_changed = [r for r in overridden if r["decision"]["stance"] != r["decision"]["llm_stance"]]
    print(f"  {len(stance_changed)} of those changed the stance, not just the tilt")

    print("\n-- llm source ------------------------------------------------")
    print(" ", dict(Counter(r["llm_source"] for r in decisions)))
    print(f"  distinct cache keys: {len({r['llm_cache_key'] for r in decisions})}")

    print("\n-- recorded token usage (from the committed cache) -----------")
    used = {r["llm_cache_key"] for r in decisions}
    tin = tout = n = 0
    for key in used:
        f = cfg.LLM_CACHE_DIR / f"{key}.json"
        if not f.exists():
            continue
        usage = json.loads(f.read_text())["response"].get("usage", {})
        tin += usage.get("input_tokens", 0)
        tout += usage.get("output_tokens", 0)
        n += 1
    if n:
        print(f"  {n} recorded calls: {tin} input + {tout} output tokens")
        print(f"  mean per call: {tin / n:.0f} in / {tout / n:.0f} out")
        # Haiku 3.5 list price at time of writing: $0.80/MTok in, $4.00/MTok out.
        cost = tin / 1e6 * 0.80 + tout / 1e6 * 4.00
        print(f"  approx list-price cost of the full demo run: ${cost:.4f}")


if __name__ == "__main__":
    main()
