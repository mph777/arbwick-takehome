"""Verify the committed snapshot before anything is allowed to trust it.

Prints a human report and writes data/snapshot/verification_report.json.
Exits non-zero if any HARD check fails, so it can gate CI.

HARD  - the snapshot is unusable as-is (integrity, ordering, arithmetic).
SOFT  - a real property of exchange history that the pipeline must handle at
        run time rather than at fetch time (a gap, a zero-volume day, a symbol
        that does not span the full window). Reported, never silently patched.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402
from pipeline.loader import load_raw  # noqa: E402

hard_failures: list[str] = []
soft_findings: list[str] = []


def hard(cond: bool, msg: str) -> None:
    if not cond:
        hard_failures.append(msg)


def soft(cond: bool, msg: str) -> None:
    if not cond:
        soft_findings.append(msg)


def check_manifest() -> dict:
    manifest = json.loads(cfg.MANIFEST_FILE.read_text())
    for symbol, meta in manifest["files"].items():
        path = cfg.SNAPSHOT_DIR / meta["file"]
        hard(path.exists(), f"{symbol}: {meta['file']} missing")
        if not path.exists():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        hard(digest == meta["sha256"],
             f"{symbol}: sha256 mismatch - file was edited after fetch "
             f"(manifest {meta['sha256'][:12]}, actual {digest[:12]})")
    return manifest


def check_symbol(symbol: str, df: pd.DataFrame) -> dict:
    n = len(df)
    hard(n > 0, f"{symbol}: empty file")
    if n == 0:
        return {}

    ot = df["open_time"]
    hard(ot.is_monotonic_increasing, f"{symbol}: open_time not increasing")
    hard(not ot.duplicated().any(),
         f"{symbol}: {int(ot.duplicated().sum())} duplicate open_time rows "
         f"(pagination overlap not de-duplicated)")

    # Every daily kline must span exactly one day minus one millisecond.
    span = df["close_time"] - df["open_time"]
    hard((span == cfg.DAY_MS - 1).all(),
         f"{symbol}: {int((span != cfg.DAY_MS - 1).sum())} rows whose "
         f"close_time-open_time != 86399999ms")

    # Candle geometry.
    hard((df[["open", "high", "low", "close"]] > 0).all().all(),
         f"{symbol}: non-positive price field")
    hard((df["high"] >= df["low"]).all(), f"{symbol}: high < low")
    hard((df["high"] >= df[["open", "close"]].max(axis=1)).all(),
         f"{symbol}: high below open/close")
    hard((df["low"] <= df[["open", "close"]].min(axis=1)).all(),
         f"{symbol}: low above open/close")
    hard((df["volume"] >= 0).all(), f"{symbol}: negative volume")
    hard((df["taker_buy_base"] <= df["volume"] * (1 + 1e-9)).all(),
         f"{symbol}: taker buy base exceeds total volume")
    hard(not df[["open", "high", "low", "close", "volume"]].isna().any().any(),
         f"{symbol}: NaN in OHLCV")

    # Window bounds.
    first, last = df["date"].iloc[0], df["date"].iloc[-1]
    hard(first >= cfg.WINDOW_START, f"{symbol}: data before window start ({first})")
    hard(last <= cfg.WINDOW_END, f"{symbol}: data past window end ({last}) - "
                                 f"the end date is hard, later data breaks reproducibility")

    # Calendar completeness. Crypto trades every calendar day, so a missing date
    # is a real hole, not a weekend.
    full = pd.date_range(first, last, freq="D").date
    missing = sorted(set(full) - set(df["date"]))
    soft(not missing, f"{symbol}: {len(missing)} missing calendar day(s): "
                      f"{[str(d) for d in missing[:10]]}")

    gaps = []
    if missing:
        run_start = prev = missing[0]
        for d in missing[1:]:
            if (d - prev).days == 1:
                prev = d
                continue
            gaps.append((run_start, prev, (prev - run_start).days + 1))
            run_start = prev = d
        gaps.append((run_start, prev, (prev - run_start).days + 1))
    max_gap = max((g[2] for g in gaps), default=0)
    soft(max_gap <= cfg.MAX_CONSECUTIVE_GAP_DAYS,
         f"{symbol}: longest gap is {max_gap} consecutive days "
         f"(pipeline refuses above {cfg.MAX_CONSECUTIVE_GAP_DAYS})")

    zero_vol = int((df["volume"] == 0).sum())
    soft(zero_vol == 0, f"{symbol}: {zero_vol} zero-volume day(s)")

    flat = int((df["high"] == df["low"]).sum())
    soft(flat == 0, f"{symbol}: {flat} day(s) with high == low (no intraday range)")

    return {
        "n_candles": n,
        "first_date": str(first),
        "last_date": str(last),
        "expected_days": len(full),
        "missing_days": [str(d) for d in missing],
        "gaps": [{"from": str(a), "to": str(b), "days": c} for a, b, c in gaps],
        "max_consecutive_gap_days": max_gap,
        "zero_volume_days": zero_vol,
        "flat_range_days": flat,
        "last_close": float(df["close"].iloc[-1]),
    }


def check_alignment(frames: dict[str, pd.DataFrame], late_symbol: str) -> dict:
    core = {s: set(f["date"]) for s, f in frames.items() if s != late_symbol}
    union = set().union(*core.values())
    report = {}
    for s, dates in core.items():
        diff = sorted(union - dates)
        soft(not diff, f"alignment: {s} misses {len(diff)} date(s) present in "
                       f"another core symbol: {[str(d) for d in diff[:5]]}")
        report[s] = {"missing_vs_union": [str(d) for d in diff]}

    late = frames[late_symbol]
    late_first = late["date"].iloc[0]
    hard(late_first > cfg.LATE_LISTING_AFTER,
         f"{late_symbol}: first candle {late_first} is not after "
         f"{cfg.LATE_LISTING_AFTER} - does not satisfy the late-listing requirement")
    soft(set(late["date"]) <= union | set(late["date"]),
         f"{late_symbol}: dates outside the core union")

    # The late symbol must genuinely start at its listing, not be back-filled.
    selection = json.loads(cfg.LATE_SYMBOL_SELECTION_FILE.read_text())["selected"]
    hard(selection["symbol"] == late_symbol,
         f"selection file says {selection['symbol']} but snapshot has {late_symbol}")
    hard(str(late_first) == selection["first_candle_date"],
         f"{late_symbol}: snapshot starts {late_first}, discovery probe found "
         f"{selection['first_candle_date']}")

    report[late_symbol] = {
        "first_date": str(late_first),
        "listing_probe_date": selection["first_candle_date"],
        "days_of_history_at_window_end": (cfg.WINDOW_END - late_first).days + 1,
    }
    return report


def main() -> None:
    manifest = check_manifest()
    late_symbol = manifest["late_symbol"]
    symbols = list(cfg.CORE_SYMBOLS) + [late_symbol]

    frames = {s: load_raw(s) for s in symbols}
    per_symbol = {s: check_symbol(s, df) for s, df in frames.items()}
    alignment = check_alignment(frames, late_symbol)

    report = {
        "snapshot_sha256": snapshot_digest(manifest),
        "per_symbol": per_symbol,
        "alignment": alignment,
        "hard_failures": hard_failures,
        "soft_findings": soft_findings,
    }
    (cfg.SNAPSHOT_DIR / "verification_report.json").write_text(json.dumps(report, indent=2))

    print("=" * 72)
    for s, r in per_symbol.items():
        if not r:
            continue
        print(f"{s:<12} {r['n_candles']:>5} candles  {r['first_date']} -> {r['last_date']}"
              f"  missing={len(r['missing_days'])}  max_gap={r['max_consecutive_gap_days']}"
              f"  zero_vol={r['zero_volume_days']}")
    print("=" * 72)
    if soft_findings:
        print("\nSOFT findings (handled at run time, not patched here):")
        for m in soft_findings:
            print(f"  - {m}")
    if hard_failures:
        print("\nHARD failures:")
        for m in hard_failures:
            print(f"  ! {m}")
        raise SystemExit(1)
    print("\nAll hard checks passed.")


def snapshot_digest(manifest: dict) -> str:
    """One hash identifying the whole snapshot, for the decision log."""
    joined = "|".join(f"{s}:{m['sha256']}" for s, m in sorted(manifest["files"].items()))
    return hashlib.sha256(joined.encode()).hexdigest()


if __name__ == "__main__":
    main()
