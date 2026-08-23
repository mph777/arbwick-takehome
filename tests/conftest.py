"""Synthetic snapshots.

Tests never touch the committed data: a test that depends on what BTC did in
2024 is a test that breaks when the snapshot is refreshed. Everything here is a
seeded random walk written in the exact on-disk format the fetcher produces, so
the loader, the quality gate and the stages are exercised for real.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402
from data.fetch import KLINE_COLUMNS  # noqa: E402
from pipeline import loader  # noqa: E402

START = date(2023, 1, 1)


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def make_rows(n_days: int, seed: int, start: date = START,
              drift: float = 0.0005, vol: float = 0.03, price0: float = 100.0):
    rng = np.random.default_rng(seed)
    rows = []
    price = price0
    for i in range(n_days):
        d = start + timedelta(days=i)
        ret = drift + vol * rng.standard_normal()
        close = price * float(np.exp(ret))
        high = max(price, close) * (1 + abs(rng.standard_normal()) * 0.004)
        low = min(price, close) * (1 - abs(rng.standard_normal()) * 0.004)
        volume = 1000 + abs(rng.standard_normal()) * 100
        ot = _ms(d)
        rows.append([
            ot, f"{price:.8f}", f"{high:.8f}", f"{low:.8f}", f"{close:.8f}",
            f"{volume:.8f}", ot + cfg.DAY_MS - 1, f"{volume * close:.8f}", 5000,
            f"{volume / 2:.8f}", f"{volume * close / 2:.8f}", "0",
        ])
        price = close
    return rows


def write_snapshot(tmp: Path, frames: dict[str, list[list]], late_symbol: str) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    manifest = {"fetched_at_utc": "synthetic", "endpoint": "synthetic",
                "interval": "1d", "window_start": str(START),
                "window_end": str(cfg.WINDOW_END), "late_symbol": late_symbol,
                "files": {}}
    for symbol, rows in frames.items():
        path = tmp / f"{symbol}.csv"
        with path.open("w", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(KLINE_COLUMNS)
            w.writerows(rows)
        manifest["files"][symbol] = {
            "file": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "n_candles": len(rows),
        }
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))
    loader.clear_cache()
    return tmp


@pytest.fixture
def snapshot(tmp_path) -> Path:
    """Three symbols with ~2 years of clean history, one listed late."""
    n = 700
    frames = {
        "BTCUSDT": make_rows(n, seed=1),
        "ETHUSDT": make_rows(n, seed=2, vol=0.04),
        "SOLUSDT": make_rows(n, seed=3, vol=0.05),
        "LATEUSDT": make_rows(60, seed=4, start=START + timedelta(days=n - 60)),
    }
    return write_snapshot(tmp_path / "snap", frames, "LATEUSDT")


@pytest.fixture
def clean_symbol(snapshot):
    """(snapshot dir, symbol, as-of date with plenty of history)."""
    return snapshot, "BTCUSDT", START + timedelta(days=650)


@pytest.fixture(autouse=True)
def _isolate_loader_cache():
    loader.clear_cache()
    yield
    loader.clear_cache()
