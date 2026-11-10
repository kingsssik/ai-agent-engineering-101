"""Week 08 -- what buyers ask *this shop* for: a demand signal the market does not publish.

The market hides which products sell. The ledger shows what settled (market_memory),
but only for SKUs some shop already stocked. Every buyer that opens a negotiation
names the SKU its order wants, so the seller's own inbox is a second, earlier signal:
a SKU asked for 400 times while the shop had none is demand nobody is serving.

The seller calls `record(sku)` for every opening request (served or not); the
restock hook calls `rates()` to get asks per hour per SKU. Counts decay with a
6-hour half-life so the signal follows the market as it moves.
"""
import json
import math
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILE = HERE / "memory" / "asks.json"
HALF_LIFE = 6 * 3600
FLUSH_EVERY = 30               # seconds between writes; the seller sees hundreds of asks an hour

_mem = None
_last_flush = 0.0


def _decay(row, now):
    f = 0.5 ** ((now - row["t"]) / HALF_LIFE)
    row["n"] *= f
    row["miss"] = row.get("miss", 0.0) * f
    row["t"] = now


def load(path=FILE):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _flush(now):
    global _last_flush
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(_mem, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, FILE)
    _last_flush = now


def record(sku, served, now=None):
    """One buyer asked for `sku`; `served` says whether the shop had a unit to offer."""
    global _mem
    now = now or time.time()
    if _mem is None:
        _mem = load()
    row = _mem.setdefault(sku, {"n": 0.0, "miss": 0.0, "t": now})
    _decay(row, now)
    row["n"] += 1
    if not served:
        row["miss"] = row.get("miss", 0.0) + 1
    if now - _last_flush > FLUSH_EVERY:
        _flush(now)


def rates(now=None, path=FILE):
    """SKU -> (asks per hour, unserved asks per hour), from the decayed counts."""
    now = now or time.time()
    out = {}
    per_hour = math.log(2) / (HALF_LIFE / 3600)      # a decayed count n is about n * this per hour
    for sku, row in load(path).items():
        f = 0.5 ** ((now - row["t"]) / HALF_LIFE)
        out[sku] = (row["n"] * f * per_hour, row.get("miss", 0.0) * f * per_hour)
    return out
