"""Fetch the committed snapshot of daily klines from the Binance public API.

Run once to produce data/snapshot/. The pipeline never calls this module - it
reads the committed CSVs. Two properties matter here:

1. The late-listed symbol is chosen *from the data*, not from memory: we probe
   the first available daily candle of every USDT spot pair and keep the ones
   that started after the cut-off. The full candidate table is committed so the
   choice is reviewable.
2. Kline fields are written verbatim as the strings the exchange returned. No
   float round-trip happens between the API and disk, so the file hash is a
   meaningful identity for "what the exchange said" and re-running the fetch is
   byte-stable.

Usage:
    python data/fetch.py                    # full fetch (uses discovery cache if present)
    python data/fetch.py --refresh-discovery
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config as cfg  # noqa: E402

KLINE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "n_trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
]

DISCOVERY_CACHE = cfg.SNAPSHOT_DIR / "_discovery_cache.json"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "arbwick-takehome/1.0"})


def to_ms(d, end_of_day: bool = False) -> int:
    dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    if end_of_day:
        dt = dt + timedelta(days=1) - timedelta(milliseconds=1)
    return int(dt.timestamp() * 1000)


def ms_to_date(ms: int):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()


def get(path: str, params: dict | None = None, retries: int = 5) -> object:
    """GET with backoff. 418/429 mean we tripped a rate limit - back off hard."""
    url = f"{cfg.BINANCE_BASE_URL}{path}"
    for attempt in range(retries):
        resp = SESSION.get(url, params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (418, 429):
            wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 2)))
            print(f"  rate limited ({resp.status_code}), sleeping {wait}s", flush=True)
            time.sleep(wait)
            continue
        if 500 <= resp.status_code < 600:
            time.sleep(2**attempt)
            continue
        raise RuntimeError(f"{resp.status_code} from {url}: {resp.text[:300]}")
    raise RuntimeError(f"exhausted retries for {url}")


# ---------------------------------------------------------------------------
# Late-symbol discovery
# ---------------------------------------------------------------------------

LEVERAGED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


def usdt_spot_symbols() -> list[dict]:
    info = get("/api/v3/exchangeInfo")
    out = []
    for s in info["symbols"]:
        if s["quoteAsset"] != "USDT":
            continue
        if s["status"] != "TRADING" or not s.get("isSpotTradingAllowed", False):
            continue
        if s["symbol"].endswith(LEVERAGED_SUFFIXES):
            continue
        out.append({"symbol": s["symbol"], "base": s["baseAsset"]})
    return sorted(out, key=lambda x: x["symbol"])


def first_candle_ms(symbol: str) -> int | None:
    """Earliest daily candle the exchange will serve for this symbol."""
    rows = get(
        "/api/v3/klines",
        {"symbol": symbol, "interval": cfg.KLINE_INTERVAL, "startTime": 0, "limit": 1},
    )
    return rows[0][0] if rows else None


def discover_late_candidates(refresh: bool) -> list[dict]:
    if DISCOVERY_CACHE.exists() and not refresh:
        print(f"using cached discovery: {DISCOVERY_CACHE}")
        return json.loads(DISCOVERY_CACHE.read_text())["candidates"]

    symbols = usdt_spot_symbols()
    print(f"probing first candle of {len(symbols)} USDT spot pairs ...", flush=True)
    cutoff_ms = to_ms(cfg.LATE_LISTING_AFTER)
    window_end_ms = to_ms(cfg.WINDOW_END, end_of_day=True)

    candidates = []
    for i, s in enumerate(symbols, 1):
        try:
            first = first_candle_ms(s["symbol"])
        except RuntimeError as exc:
            print(f"  skip {s['symbol']}: {exc}")
            continue
        if first is None or first <= cutoff_ms or first > window_end_ms:
            continue
        candidates.append(
            {"symbol": s["symbol"], "base": s["base"], "first_candle_ms": first,
             "first_candle_date": ms_to_date(first).isoformat()}
        )
        if i % 50 == 0:
            print(f"  {i}/{len(symbols)} probed, {len(candidates)} late listings", flush=True)
        time.sleep(0.05)

    candidates.sort(key=lambda c: c["symbol"])
    cfg.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    DISCOVERY_CACHE.write_text(
        json.dumps({"probed_at": datetime.now(timezone.utc).isoformat(),
                    "n_symbols_probed": len(symbols),
                    "candidates": candidates}, indent=2)
    )
    return candidates


def median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def select_late_symbol(candidates: list[dict]) -> tuple[dict, list[dict]]:
    """Rank post-cutoff listings by liquidity *inside the mandated window*.

    Deliberately NOT ranked by /ticker/24hr quote volume. That figure is a
    rolling 24-hour window measured at request time, so two candidates with
    similar turnover can swap places between runs and the fetch would produce a
    different snapshot each time - the selection would not be reproducible, which
    is the property the whole exercise is about.

    The ranking key here is the median daily quote volume over the symbol's
    history inside the fixed window, computed from the same candles that are
    fetched anyway to verify completeness. Given the window, it is a constant.

    Residual non-determinism, stated rather than hidden: a symbol delisted
    between runs disappears from exchangeInfo and so from the candidate set.
    `cfg.LATE_SYMBOL_PIN` closes that - once chosen, the symbol is pinned and
    discovery becomes the justification for the choice rather than a live
    dependency.
    """
    window_end_ms = to_ms(cfg.WINDOW_END, end_of_day=True)
    scored: list[dict] = []

    print(f"scoring {len(candidates)} late listings on in-window liquidity ...", flush=True)
    for i, c in enumerate(candidates, 1):
        rows = fetch_klines(c["symbol"], c["first_candle_ms"], window_end_ms)
        if not rows:
            continue
        expected = (ms_to_date(rows[-1][0]) - ms_to_date(rows[0][0])).days + 1
        entry = dict(c)
        entry["n_candles"] = len(rows)
        entry["expected_candles"] = expected
        entry["complete_history"] = len(rows) == expected
        entry["last_date"] = ms_to_date(rows[-1][0]).isoformat()
        entry["median_daily_quote_volume"] = median([float(r[7]) for r in rows])
        entry["_rows"] = rows
        scored.append(entry)
        if i % 10 == 0:
            print(f"  {i}/{len(candidates)} scored", flush=True)

    # Deterministic order: liquidity descending, symbol as the tie-break so that
    # even an exact tie cannot reorder between runs.
    scored.sort(key=lambda c: (-c["median_daily_quote_volume"], c["symbol"]))

    if cfg.LATE_SYMBOL_PIN:
        pinned = [c for c in scored if c["symbol"] == cfg.LATE_SYMBOL_PIN]
        if not pinned:
            raise RuntimeError(
                f"LATE_SYMBOL_PIN={cfg.LATE_SYMBOL_PIN} is not among the "
                f"{len(scored)} symbols listed after {cfg.LATE_LISTING_AFTER}"
            )
        chosen = pinned[0]
        if not chosen["complete_history"]:
            raise RuntimeError(f"{chosen['symbol']}: history has holes "
                               f"({chosen['n_candles']} candles vs "
                               f"{chosen['expected_candles']} days)")
        chosen["selection_reason"] = (
            f"pinned in config.py; rank {scored.index(chosen) + 1} of {len(scored)} "
            f"by median daily quote volume inside the window"
        )
    else:
        chosen = next((c for c in scored if c["complete_history"]), None)
        if chosen is None:
            raise RuntimeError("no late-listed candidate had a complete daily history")
        for c in scored[:scored.index(chosen)]:
            print(f"  {c['symbol']} skipped: {c['n_candles']} candles vs "
                  f"{c['expected_candles']} expected days")
        chosen["selection_reason"] = (
            "highest median daily quote volume inside the mandated window, among "
            "post-cutoff listings with an unbroken daily history"
        )

    return chosen, scored


# ---------------------------------------------------------------------------
# Kline fetch
# ---------------------------------------------------------------------------


def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> list[list]:
    """Page through /api/v3/klines.

    startTime is inclusive on open_time, so the cursor advances by exactly one
    interval past the last row received. Rows are de-duplicated on open_time
    anyway - overlapping pages are a normal outcome of a naive cursor and must
    not silently double-count.
    """
    seen: dict[int, list] = {}
    cursor = start_ms
    while cursor <= end_ms:
        batch = get(
            "/api/v3/klines",
            {"symbol": symbol, "interval": cfg.KLINE_INTERVAL, "startTime": cursor,
             "endTime": end_ms, "limit": cfg.KLINES_LIMIT},
        )
        if not batch:
            break
        for row in batch:
            seen.setdefault(row[0], row)
        cursor = batch[-1][0] + cfg.DAY_MS
        if len(batch) < cfg.KLINES_LIMIT:
            break
        time.sleep(0.1)
    return [seen[k] for k in sorted(seen)]


def drop_unclosed(rows: list[list], symbol: str) -> list[list]:
    """Discard any candle that had not closed at fetch time.

    Irrelevant for a window that ended in the past, but a fetch script that can
    silently capture a forming candle is a live foot-gun, so it is handled here
    rather than assumed away.
    """
    now_ms = int(time.time() * 1000)
    closed = [r for r in rows if r[6] <= now_ms]
    if len(closed) != len(rows):
        print(f"  {symbol}: dropped {len(rows) - len(closed)} unclosed candle(s)")
    return closed


def write_csv(symbol: str, rows: list[list]) -> Path:
    path = cfg.SNAPSHOT_DIR / f"{symbol}.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(KLINE_COLUMNS)
        for r in rows:
            # Verbatim: ints stay ints, decimal strings stay strings.
            w.writerow(r)
    return path


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-discovery", action="store_true",
                    help="re-probe every USDT pair instead of using the cached table")
    args = ap.parse_args()

    cfg.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    candidates = discover_late_candidates(args.refresh_discovery)
    if not candidates:
        raise SystemExit("no symbol listed after the cut-off was found")
    chosen, scored = select_late_symbol(candidates)
    print(f"late symbol: {chosen['symbol']} (listed {chosen['first_candle_date']}, "
          f"{chosen['n_candles']} candles)")

    strip = lambda c: {k: v for k, v in c.items() if not k.startswith("_")}
    cfg.LATE_SYMBOL_SELECTION_FILE.write_text(
        json.dumps(
            {"selected": strip(chosen),
             "criteria": {
                 "listed_after": cfg.LATE_LISTING_AFTER.isoformat(),
                 "must_be_usdt_spot_trading": True,
                 "must_have_unbroken_daily_history": True,
                 "ranking_key": "median daily quote volume inside the mandated "
                                "window (window-determined, not live 24h volume)",
                 "tie_break": "symbol, ascending",
                 "pinned": cfg.LATE_SYMBOL_PIN},
             "all_candidates": [strip(c) for c in scored]},
            indent=2,
        )
    )

    start_ms = to_ms(cfg.WINDOW_START)
    end_ms = to_ms(cfg.WINDOW_END, end_of_day=True)
    symbols = list(cfg.CORE_SYMBOLS) + [chosen["symbol"]]

    manifest = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "endpoint": f"{cfg.BINANCE_BASE_URL}/api/v3/klines",
        "interval": cfg.KLINE_INTERVAL,
        "window_start": cfg.WINDOW_START.isoformat(),
        "window_end": cfg.WINDOW_END.isoformat(),
        "late_symbol": chosen["symbol"],
        "files": {},
    }

    for symbol in symbols:
        print(f"fetching {symbol} ...", flush=True)
        if symbol == chosen["symbol"]:
            # Already fetched while scoring candidates; re-requesting it would be
            # a second, differently-timed view of the same window.
            rows = chosen["_rows"]
        else:
            rows = fetch_klines(symbol, start_ms, end_ms)
        rows = drop_unclosed(rows, symbol)
        if not rows:
            raise SystemExit(f"{symbol}: no candles returned")
        path = write_csv(symbol, rows)
        manifest["files"][symbol] = {
            "file": path.name,
            "sha256": sha256_file(path),
            "n_candles": len(rows),
            "first_date": ms_to_date(rows[0][0]).isoformat(),
            "last_date": ms_to_date(rows[-1][0]).isoformat(),
            "bytes": path.stat().st_size,
        }
        print(f"  {len(rows)} candles "
              f"{manifest['files'][symbol]['first_date']} -> "
              f"{manifest['files'][symbol]['last_date']}")

    cfg.MANIFEST_FILE.write_text(json.dumps(manifest, indent=2))
    print(f"\nwrote {cfg.MANIFEST_FILE}")
    print("next: python data/verify.py")


if __name__ == "__main__":
    main()
