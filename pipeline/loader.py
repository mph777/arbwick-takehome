"""Snapshot loading and the one and only point-in-time cut.

Every stage receives an already-truncated frame. No stage is given the full
history and trusted to slice it correctly, because "each stage slices it
correctly" is exactly the invariant that rots the moment someone adds a fourth
stage. There is one `as_of_cutoff_ms` per run, computed in the orchestrator and
applied here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd

import config as cfg

NUMERIC_COLUMNS = [
    "open", "high", "low", "close", "volume",
    "quote_volume", "n_trades", "taker_buy_base", "taker_buy_quote",
]


def as_of_cutoff_ms(as_of: date) -> int:
    """Milliseconds at 00:00:00.000 UTC of the day AFTER `as_of`.

    A decision dated t is stamped at that instant and may use every candle that
    has closed by then. Daily candle t closes at 23:59:59.999 UTC of t, so it is
    included; candle t+1 has not opened. Expressing the boundary as a strict
    `<` against the next midnight avoids depending on whether the exchange's
    close_time is millisecond- or microsecond-precise.
    """
    dt = datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc) + timedelta(days=1)
    return int(dt.timestamp() * 1000)


@lru_cache(maxsize=32)
def _read(snapshot_dir_str: str, symbol: str) -> pd.DataFrame:
    path = Path(snapshot_dir_str) / f"{symbol}.csv"
    if not path.exists():
        raise FileNotFoundError(f"no snapshot for {symbol}: {path}")
    df = pd.read_csv(path)
    df["open_time"] = df["open_time"].astype("int64")
    df["close_time"] = df["close_time"].astype("int64")
    for col in NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.date
    return df.sort_values("open_time").reset_index(drop=True)


def load_raw(symbol: str, snapshot_dir: Path | None = None) -> pd.DataFrame:
    """Full committed history for a symbol. Only fetch/verify tooling uses this."""
    d = snapshot_dir or cfg.SNAPSHOT_DIR
    return _read(str(d), symbol).copy()


def load_as_of(symbol: str, cutoff_ms: int, snapshot_dir: Path | None = None) -> pd.DataFrame:
    """Every candle that had closed before `cutoff_ms`, and nothing else."""
    df = _read(str(snapshot_dir or cfg.SNAPSHOT_DIR), symbol)
    return df[df["close_time"] < cutoff_ms].reset_index(drop=True).copy()


def universe(snapshot_dir: Path | None = None) -> list[str]:
    manifest = json.loads((snapshot_dir or cfg.SNAPSHOT_DIR).joinpath("manifest.json").read_text())
    late = manifest["late_symbol"]
    return list(cfg.CORE_SYMBOLS) + ([late] if late not in cfg.CORE_SYMBOLS else [])


def snapshot_sha256(snapshot_dir: Path | None = None) -> str:
    """Identity of the snapshot as a whole, recorded on every decision."""
    d = snapshot_dir or cfg.SNAPSHOT_DIR
    manifest = json.loads((d / "manifest.json").read_text())
    joined = "|".join(f"{s}:{m['sha256']}" for s, m in sorted(manifest["files"].items()))
    return hashlib.sha256(joined.encode()).hexdigest()


def clear_cache() -> None:
    _read.cache_clear()
