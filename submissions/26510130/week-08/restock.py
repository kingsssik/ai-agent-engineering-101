"""Week 08 -- the restock hook: what the shop buys, from what the market has bought.

    python restock.py --state .market        # the kit runs it every 60 s

Prints one line, {"buy": [{"sku": ..., "qty": n}, ...]} (or "bankrupt": true).

Stock that never sells is the whole capital lost, and the market hides what
sells. So the restock does not guess: it reads `market_memory` (built from the
ledger's settled deals) and stocks the categories that are selling now, in the
SKUs that sold. Before there is enough data (the first hours of the round) it
seeds one unit in many cheap, slow-to-spoil categories, because a shop with
nothing in an order's category is not even shown to that buyer.

Limits live here, in code, not in a prompt:
  - a cash floor the shop never spends below,
  - a cap on stock held at cost,
  - no short-shelf food unless that category is clearly selling,
  - no buying at all in the last hours of the round, when stock can only be lost.
Every decision is appended to logs/restock.jsonl with its reason.
"""
import argparse
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import asks
import market_memory as mm

HERE = Path(__file__).resolve().parent
LOG = HERE / "logs" / "restock.jsonl"
ROUND_END = float(os.environ.get("ROUND_END", datetime(2026, 11, 17, 14, 59, tzinfo=timezone.utc).timestamp()))
CASH_FLOOR = 1_500_000         # never spend below this
STOCK_CAP = 2_500_000          # stock held, at cost: the floor of the cap below
STOCK_SHARE = 0.35             # ... which grows with the balance (11-10: 20 M cash sat idle under a 2.5 M cap)
STOCK_CAP_MAX = 10_000_000
ASK_MIN_RATE = 0.5             # asks/h before a SKU asked of this shop is stocked
ASK_SELL_THROUGH = 0.3         # deals per ask: 753 deals from 2,490 negotiations, 11-08..11-10
ASK_HORIZON_H = 1.5            # stock for this long: a supply window opens every 20 min, but not always on time
ASK_MAX_QTY = 20               # the market's per-line limit
SURGE_MAX = 1.15               # skip a SKU whose market-wide surge has pushed cost up more than this
SEED_SPEND = 1_200_000         # cold start: at most this much, spread wide
COLD_SETTLES = 40              # market settles seen before demand is trusted
MAX_LIST = 400_000             # dearer SKUs only when their category is selling
STOP_BEFORE_END = 36 * 3600    # no buying in the last 36 h
LINES = 10                     # the kit buys at most 10 lines a run
EST_COST = 0.6                 # expected cost / list price before the unit arrives


def say(msg):
    print(msg, file=sys.stderr, flush=True)


def read(state, name, default):
    try:
        return json.loads((Path(state) / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def leaf(category):
    return (category or "").split(">")[-1]


def nearest_median(skus, median, cat):
    """The SKU whose list price is closest to the category median: an order's acceptable
    SKUs are priced 0.5-1.5x a reference, so the middle of the category fits most orders."""
    return sorted(skus, key=lambda s: abs(math.log(max(cat[s]["list_price"], 1) / max(median, 1))))


def plan(inv, demand, cat, mem, now):
    units = [u for u in inv.get("units", []) if u.get("grade") != "expired"]
    balance = inv.get("balance") or 0
    held = sum(u.get("cost") or 0 for u in units)
    have = Counter(u.get("category") for u in units)
    have_sku = Counter(u.get("sku") for u in units)
    by_cat = defaultdict(list)
    for s, x in cat.items():
        by_cat[x["category"]].append(s)
    med = {d["category"]: d["median_price"] for d in demand}
    shelf = {d["category"]: min((cat[s].get("shelf_hours") or 1e9) for s in by_cat[d["category"]])
             for d in demand if by_cat.get(d["category"])}

    if ROUND_END - now < STOP_BEFORE_END:
        return [], "last hours of the round: selling down, buying nothing"
    cap = min(STOCK_CAP_MAX, max(STOCK_CAP, STOCK_SHARE * (balance + held)))
    room = min(balance - CASH_FLOOR, cap - held)
    if room <= 0:
        return [], f"no room: balance {balance}, stock at cost {held}, cap {cap:.0f}"

    settles = mem.get("seen", {}).get("settle", 0)
    buy, why = [], []
    surge = {s["sku"]: s.get("factor", 1.0) for s in inv.get("surge") or []}

    # 1. What buyers asked this shop for (asks.py): demand seen directly, SKU by SKU.
    spent = 0
    for sku, (rate, miss) in sorted(asks.rates(now).items(), key=lambda kv: -kv[1][0]):
        if rate < ASK_MIN_RATE or len(buy) >= LINES or sku not in cat:
            continue
        x = cat[sku]
        if surge.get(sku, 1.0) > SURGE_MAX:
            why.append(f"skip {sku} surge {surge[sku]}")
            continue
        horizon = ASK_HORIZON_H
        if x.get("kind") == "food" and x.get("shelf_hours"):
            horizon = min(horizon, x["shelf_hours"] * 0.25)      # sell well inside the shelf life
        target = min(ASK_MAX_QTY, math.ceil(rate * ASK_SELL_THROUGH * horizon))
        qty = target - have_sku[sku]
        lp = x["list_price"]
        if qty < 1 or (lp > MAX_LIST and rate < 3):
            continue
        qty = min(qty, int((room - spent) / (lp * EST_COST * surge.get(sku, 1.0))))
        if qty < 1:
            continue
        buy.append({"sku": sku, "qty": qty})
        why.append(f"asked {sku} x{qty} ({rate:.1f}/h, {miss:.1f} unserved/h, have {have_sku[sku]})")
        spent += lp * EST_COST * surge.get(sku, 1.0) * qty
        have[x["category"]] += qty
    room -= spent
    if len(buy) >= LINES or room <= 0:
        return buy, "asks: " + "; ".join(why)
    if settles < COLD_SETTLES:
        # Seed: one unit in many categories, cheapest-to-hold first (goods, then long-shelf food).
        spent = 0
        seedable = [d for d in demand if d["median_price"] <= 120_000 and shelf.get(d["category"], 1e9) >= 168]
        seedable.sort(key=lambda d: (-d["skus"], d["median_price"]))   # many SKUs: a big category
        for d in seedable:
            c = d["category"]
            if have[c] or not by_cat.get(c):
                continue
            sku = nearest_median(by_cat[c], d["median_price"], cat)[0]
            est = cat[sku]["list_price"] * EST_COST
            if spent + est > min(room, SEED_SPEND - held):
                continue
            buy.append({"sku": sku, "qty": 1})
            why.append(f"seed {leaf(c)}")
            spent += est
            if len(buy) >= LINES:
                break
        return buy, f"cold start ({settles} settles seen): " + ", ".join(why)

    # Demand-led: categories by decayed market sales, SKUs that actually sold first.
    hot = [(c, s) for c, s in mm.hot_categories(mem, 40) if c in by_cat and s >= 1]
    hot_sku = dict(mm.hot_skus(mem, 400))
    spent = 0
    for c, score in hot:
        short = shelf.get(c, 1e9) < 96
        if short and score < 30:
            continue                                         # spoils before it sells
        target = max(1, min(6 if not short else 2, math.ceil(score / 12)))
        need = target - have[c]
        if need <= 0:
            continue
        skus = sorted(by_cat[c], key=lambda s: -hot_sku.get(s, 0))
        if not hot_sku.get(skus[0]):
            skus = nearest_median(by_cat[c], med.get(c, 20_000), cat)
        for sku in skus[:2]:
            lp = cat[sku]["list_price"]
            if lp > MAX_LIST and score < 5:
                continue
            if surge.get(sku, 1.0) > SURGE_MAX or any(b["sku"] == sku for b in buy):
                continue
            qty = min(need, 3, max(1, int((room - spent) / (lp * EST_COST))))
            est = lp * EST_COST * qty
            if qty < 1 or spent + est > room:
                continue
            buy.append({"sku": sku, "qty": qty})
            why.append(f"{leaf(c)} x{qty} (score {score:.1f}, have {have[c]}, sku sold {hot_sku.get(sku, 0):.1f})")
            spent += est
            need -= qty
            if need <= 0 or len(buy) >= LINES:
                break
        if len(buy) >= LINES:
            break
    return buy, "demand: " + "; ".join(why) if why else "demand: every hot category is stocked"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=".market")
    a = ap.parse_args()
    now = time.time()
    inv = read(a.state, "inventory.json", {})
    demand = read(a.state, "demand.json", [])
    out = {"buy": []}
    try:
        mem = mm.update(a.state, log=say)
    except Exception as e:                                   # noqa: BLE001 - stale memory beats none
        say(f"[restock] memory update failed: {type(e).__name__}: {e}")
        mem = mm.load()
    if inv.get("round", 0) < 3 or not demand:
        reason = "not Round 3 yet"
    elif (inv.get("balance") or 0) < inv.get("bankrupt_line", 0) and inv.get("bankruptcies_left"):
        sellable = [u for u in inv.get("units", []) if u.get("grade") != "expired"]
        if len(sellable) < 3:
            out["bankrupt"] = True
        reason = f"below the bankruptcy line with {len(sellable)} units left"
    else:
        cat = mm.catalog(a.state)
        out["buy"], reason = plan(inv, demand, cat, mem, now)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "balance": inv.get("balance"),
                            "units": len(inv.get("units", [])), "buy": out["buy"], "why": reason},
                           ensure_ascii=False) + "\n")
    say(f"[restock] {reason}")
    try:
        import forum
        forum.tick(a.state, mem)
    except Exception as e:                                   # noqa: BLE001 - the forum never blocks a restock
        say(f"[forum] {type(e).__name__}: {e}")
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
