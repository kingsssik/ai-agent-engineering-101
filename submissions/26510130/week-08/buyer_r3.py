"""Week 08 -- the Round 3 buyer: surplus first, and it remembers what it was sold.

    python buyer_r3.py --shops .market/shops.json --item 김치 --budget 30000 --order .market/order.json

A settled deal is worth the SKU's list price to this buyer -- or nothing, if the
food did not last `use_within_hours` or the goods broke. Surplus is list price
minus price paid. So the buyer does not ask "which shop", it asks "which unit of
which acceptable SKU at which shop", and ranks (shop, SKU) pairs by expected
surplus:

    list price - expected price      expected price from what I paid that shop
                                     before, else what the market pays (ledger)
    x  chance it is worth anything   the shop's break / dishonesty / refusal
                                     record, mine first, then the market's

Context, in three layers that never put raw logs into a prompt:
  memory/buyer.json      my purchases, and what became of them: the kit's
                         payments.jsonl (paid, or why not) and broken.jsonl
                         (only a name and a time -- this file is what links a
                         broken item back to the shop that sold it).
  memory/market.json     market_memory: prices paid and honesty, from the ledger.
  shops.json / order     this order.

The harness chooses among the five best pairs and haggles; code holds the cap
(min of budget and 95 % of list price -- above that the deal has no surplus),
refuses any accept above it, and sets the `accept` policy the kit checks before
paying: a market certificate always, enough shelf life for food, grade A/B goods.
"""
import argparse
import json
import statistics
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

import harness
import market_memory as mm

HERE = Path(__file__).resolve().parent
MEM = HERE / "memory" / "buyer.json"
HEADERS = {"A2A-Version": "1.0"}
MAX_ROUNDS = 7
MAX_SHOPS = 3                  # (shop, SKU) pairs tried per order before giving up
DEADLINE = 230                 # seconds; the kit stops a buyer at 300
CAP_SHARE = 0.95               # never pay more than this share of list price
SHOWN = 5                      # pairs put in front of the harness
T0 = time.time()


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ memory

def load_mem():
    try:
        d = json.loads(MEM.read_text(encoding="utf-8"))
        if "shops" in d:
            return d
    except (OSError, ValueError):
        pass
    return {"shops": {}, "purchases": [], "offsets": {}, "orders": 0}


def save_mem(m):
    MEM.parent.mkdir(parents=True, exist_ok=True)
    m["purchases"] = m["purchases"][-800:]
    tmp = MEM.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(MEM)


def shop_row(m, name):
    return m["shops"].setdefault(name, {"deals": 0, "paid": 0, "ratios": [], "no_deal": 0, "refused": 0,
                                        "dishonest": 0, "broken": 0, "spoiled": 0, "last": None})


def new_lines(m, path, key):
    """Lines appended to a kit file since the last order (byte offset kept in memory)."""
    p = Path(path)
    if not p.exists():
        return []
    off = m["offsets"].get(key, 0)
    size = p.stat().st_size
    if size < off:
        off = 0
    with p.open("rb") as f:
        f.seek(off)
        data = f.read()
    m["offsets"][key] = off + len(data)
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def absorb(m, state):
    """What happened after earlier orders: the kit paid or refused, and goods broke."""
    by_order = {p["order"]: p for p in m["purchases"]}
    for e in new_lines(m, Path(state) / "payments.jsonl", "payments"):
        p = by_order.get(e.get("order"))
        if not p:
            continue
        row = shop_row(m, p["shop"])
        if e.get("paid"):
            p["paid"], p["ledger"] = True, e.get("ledger")
            row["paid"] += 1
        else:
            p["paid"], p["reason"] = False, e.get("reason")
            row["refused"] += 1
            row["last"] = f"refused: {str(e.get('reason'))[:80]}"
            log(f"[buyer] memory: {p['shop']} refused at payment: {e.get('reason')}")
    for e in new_lines(m, Path(state) / "broken.jsonl", "broken"):
        cands = [p for p in m["purchases"] if p.get("paid") and p.get("name") == e.get("name")
                 and 0 <= e.get("broke_at", 0) - p["ts"] < 8 * 3600]
        if cands:
            p = max(cands, key=lambda p: p["ts"])
            p["broken"] = True
            shop_row(m, p["shop"])["broken"] += 1
            log(f"[buyer] memory: {e.get('name')} from {p['shop']} broke"
                + (" (warranty)" if p.get("warranty") else " (no warranty)"))


# ------------------------------------------------------------------ choosing

def expected_ratio(m, mkt, shop, category):
    r = shop_row(m, shop)["ratios"]
    return statistics.median(r[-20:]) if len(r) >= 3 else mm.paid_ratio(mkt, category, default=0.8)


def risk(m, mkt, shop, kind):
    """Chance the deal turns out worthless or never settles, from my record, then the market's."""
    row = shop_row(m, shop)
    tried = row["deals"] + row["no_deal"]
    fail = (row["refused"] + 0.5) / (row["deals"] + 2)       # agreed but not paid
    bad = (row["broken"] + row["spoiled"] + row["dishonest"] * 2) / (row["paid"] + 3)
    pub = mm.honesty(mkt, shop)
    if pub["sold"] >= 10:
        bad = max(bad, pub["dishonest"] / pub["sold"])
        if kind == "goods" and pub["goods"] >= 10:
            bad = max(bad, pub["broken"] / pub["goods"])
    walk = (row["no_deal"] + 1) / (tried + 3)                 # no agreement at all
    return min(0.95, fail * 0.5 + bad + walk * 0.3)


def candidates(order, shops, cat, m, mkt):
    acceptable = set(order.get("acceptable") or [])
    rows = []
    for s in shops:
        for o in s.get("offers") or []:
            if o.get("sku") not in acceptable or not o.get("qty"):
                continue
            info = cat.get(o["sku"]) or {}
            lp = info.get("list_price") or 0
            if not lp:
                continue
            ratio = expected_ratio(m, mkt, s["name"], info.get("category"))
            want = lp * ratio
            if lp * 0.5 > order["budget"]:
                continue                                     # out of reach even at half price
            price = min(order["budget"], want)
            rk = risk(m, mkt, s["name"], order.get("kind"))
            if want > order["budget"]:                       # the shop likely wants more than I may pay
                rk = 1 - (1 - rk) * (order["budget"] / want) ** 4
            stars = [r.get("stars") for r in s.get("reviews") or [] if isinstance(r.get("stars"), (int, float))]
            rows.append({"shop": s["name"], "url": s["url"], "sku": o["sku"], "name": o.get("name") or info.get("name"),
                         "list": lp, "exp_price": int(price), "risk": round(rk, 2),
                         "stars": round(statistics.mean(stars), 1) if stars else None,
                         "score": (lp - price) * (1 - rk)})
    return sorted(rows, key=lambda r: -r["score"])


def choose(order, rows):
    top = rows[:SHOWN]
    if len(top) <= 1:
        return top
    listing = "\n".join(f"{i}. {r['shop']} | {r['name'][:50]} | list {r['list']} | expected price {r['exp_price']}"
                        f" | risk {r['risk']} | stars {r['stars']}" for i, r in enumerate(top))
    d = harness.ask(
        "You are a buyer agent. One order, several (shop, product) options. A settled deal is worth the "
        "product's list price to you; your gain is list price minus what you pay, lost entirely if the item "
        "breaks or spoils. 'risk' is the estimated chance of that or of no settlement.\n"
        f"Order: {order.get('item')}, budget {order['budget']}"
        + (f", must last {order['use_within_hours']} h" if order.get("use_within_hours") else "") + "\n"
        f"Options:\n{listing}\n\n"
        'Reply with one JSON object and nothing else: {"choice": <number>, "why": "<short>"}.',
        want=("choice",), tag=" pick")
    i = harness.as_int(d.get("choice"))
    if i is None or not 0 <= i < len(top):
        log("[buyer] harness gave no usable choice; taking the best expected surplus")
        return rows
    log(f"[buyer] harness picked {i}: {top[i]['shop']} {top[i]['sku']} ({d.get('why')})")
    return [top[i]] + [r for j, r in enumerate(rows) if j != i]


# ------------------------------------------------------------------ talking

class Shop:
    def __init__(self, base):
        self.base = str(base).rstrip("/")
        self.rpc = self.base + "/"
        card = httpx.get(self.base + "/.well-known/agent-card.json", headers=HEADERS, timeout=30).json()
        iface = (card.get("supportedInterfaces") or [{}])[0]
        self.rpc = iface.get("url") or card.get("url") or self.rpc

    def send(self, text, task_id=None):
        msg = {"role": "ROLE_USER", "messageId": str(uuid.uuid4()), "parts": [{"text": text}]}
        if task_id:
            msg["taskId"] = task_id
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4())[:8], "method": "SendMessage", "params": {"message": msg}}
        log(f"[buyer] -> {text[:120]!r}")
        r = httpx.post(self.rpc, json=body, headers=HEADERS, timeout=120).json()
        if "error" in r:
            log(f"[buyer] <- error {str(r['error'].get('message'))[:120]}")
            return None
        return r["result"].get("task", r["result"])


def state_of(t):
    return (t or {}).get("status", {}).get("state", "")


def text_of(t):
    return " ".join(p.get("text", "") for p in (t or {}).get("status", {}).get("message", {}).get("parts", []))


def receipt_of(t):
    for a in (t or {}).get("artifacts", []):
        for p in a.get("parts", []):
            if isinstance(p.get("data"), dict) and "price" in p["data"]:
                return p["data"]
    return None


def ask_of(t, name=""):
    import re
    txt = text_of(t).replace(name, " ")
    nums = re.findall(r"(?:asking|counteroffer|price|for)\D{0,12}(\d[\d,]*)", txt, re.I) or re.findall(r"\d[\d,]{2,}", txt)
    return int(nums[-1].replace(",", "")) if nums else None


HAGGLE = """You are a buyer agent haggling for one unit.
Product: {name}, list price {list}. Your hard cap is {cap} (never offer above it).
What the shop said: {said}
The shop is asking {ask}. Your last offer: {last}. Round {round} of {max_rounds}.
Every won below list price is your gain, but no deal gains nothing. Shops here usually settle near {ratio:.0%} of list.
Reply with one JSON object and nothing else, one of:
{{"action": "counter", "price": <whole number>}}
{{"action": "accept"}}           (pay the shop's current ask)
{{"action": "walk_away"}}"""


def haggle(row, order, mkt, ask, last, rnd, said):
    cap = min(order["budget"], int(row["list"] * CAP_SHARE))
    d = harness.ask(HAGGLE.format(name=row["name"][:60], list=row["list"], cap=cap, said=said[:200], ask=ask,
                                  last=last, round=rnd, max_rounds=MAX_ROUNDS,
                                  ratio=mm.paid_ratio(mkt, (mm_cat(row) or ""), 0.8)),
                    want=("action",), tag=" haggle")
    action, price = str(d.get("action", "")).lower(), harness.as_int(d.get("price"))
    if action == "accept" and (ask is None or ask > cap):
        log(f"[buyer] REFUSED harness accept at {ask} > cap {cap}")
        action, price = "counter", None
    if action not in ("accept", "counter", "walk_away"):
        action = "accept" if ask is not None and ask <= cap else "counter"
    if action == "accept":
        return "accept", ask
    if action == "walk_away" and rnd >= 3:
        return "walk_away", None
    floor = int(row["list"] * 0.5)
    if price is None:
        price = int(row["list"] * (0.55 + 0.07 * rnd))
    price = max(floor if last is None else last, min(price, cap))   # never below my own last offer
    if ask is not None and price >= ask:
        return ("accept", ask) if ask <= cap else ("counter", cap)
    return "counter", price


CATS = {}


def mm_cat(row):
    return (CATS.get(row["sku"]) or {}).get("category")


def trade(row, order, mkt):
    """One shop. Returns (receipt, agreed, rounds, note)."""
    shop = Shop(row["url"])
    t = shop.send(f"I want to buy {row['name']} ({row['sku']})")
    if t is None or state_of(t) != "TASK_STATE_INPUT_REQUIRED":
        return None, None, 0, f"shop declined: {text_of(t)[:80]}"
    task_id, last, rnd = t.get("id"), None, 0
    said = text_of(t)
    while state_of(t) == "TASK_STATE_INPUT_REQUIRED" and rnd < MAX_ROUNDS and time.time() - T0 < DEADLINE:
        rnd += 1
        ask = ask_of(t, row["name"])
        action, price = haggle(row, order, mkt, ask, last, rnd, said)
        if action == "walk_away":
            return None, None, rnd, f"walked away at ask {ask}"
        cap = min(order["budget"], int(row["list"] * CAP_SHARE))
        assert price is not None and price <= cap, "code guard: never offer above the cap"
        last = price
        t = shop.send(str(price), task_id)
        if t is None:
            break
        said = text_of(t)
        log(f"[buyer] <- {state_of(t)} | {said[:140]}")
    if state_of(t) == "TASK_STATE_COMPLETED":
        return receipt_of(t), last, rnd, "deal"
    return None, None, rnd, f"no deal ({state_of(t) or 'error'})"


def policy(order, mkt):
    if order.get("kind") == "food":
        return {"cert": True, "min_hours_left": (order.get("use_within_hours") or 0) + 1}
    grades = ["A", "B"] if mm.break_rate(mkt, "B") < 0.15 else ["A"]
    return {"cert": True, "grades": grades}


def main_r3(a, order):
    m, mkt = load_mem(), mm.load()
    m["orders"] = m.get("orders", 0) + 1
    absorb(m, Path(a.order).parent)
    cat = mm.catalog(Path(a.order).parent)
    CATS.update(cat)
    shops = json.loads(Path(a.shops).read_text(encoding="utf-8"))
    rows = candidates(order, shops, cat, m, mkt)
    log(f"[buyer] order {order.get('order')} {order.get('item')} budget {order['budget']}: "
        f"{len(rows)} (shop, sku) pairs in stock")
    result = {"shop": shops[0]["url"] if shops else "", "receipt": None, "stars": 3,
              "review": "No deal: nothing in stock fit the order.", "accept": policy(order, mkt)}
    if not rows:
        save_mem(m)
        return result
    tried = set()
    for row in choose(order, rows):
        if (row["shop"], row["sku"]) in tried or len(tried) >= MAX_SHOPS or time.time() - T0 > DEADLINE:
            continue
        tried.add((row["shop"], row["sku"]))
        srow = shop_row(m, row["shop"])
        try:
            rec, agreed, rounds, note = trade(row, order, mkt)
        except Exception as e:                               # noqa: BLE001 - next shop
            rec, agreed, rounds, note = None, None, 0, f"{type(e).__name__}"
        result["shop"] = row["url"]
        if rec is None:
            srow["no_deal"] += 1
            srow["last"] = note
            log(f"[buyer] {row['shop']}: {note}")
            result["stars"], result["review"] = 3, f"No deal on {row['name'][:40]}: {note}."
            continue
        price = harness.as_int(rec.get("price"))
        if price != agreed or rec.get("item") not in (order.get("acceptable") or []):
            srow["dishonest"] += 1
            log(f"[buyer] RECEIPT MISMATCH: agreed {agreed}, receipt {rec.get('item')} at {price}")
            result.update(stars=1, review=f"Agreed {agreed}, the receipt said {price}. Not paying.")
            continue
        srow["deals"] += 1
        srow["ratios"] = (srow["ratios"] + [round(price / row["list"], 3)])[-50:]
        srow["last"] = f"deal at {price} of list {row['list']}"
        m["purchases"].append({"order": order.get("order"), "shop": row["shop"], "sku": row["sku"],
                               "name": row["name"], "kind": order.get("kind"), "price": price,
                               "list": row["list"], "ts": time.time(), "rounds": rounds,
                               "warranty": rec.get("warranty_hours")})
        result.update(receipt=rec, stars=4, review=f"Deal on {row['name'][:40]} at {price}; receipt matched.")
        break
    save_mem(m)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shops", required=True)
    ap.add_argument("--item", required=True)
    ap.add_argument("--budget", type=int, required=True)
    ap.add_argument("--order", default="")
    a = ap.parse_args()
    order = {}
    if a.order and Path(a.order).exists():
        order = json.loads(Path(a.order).read_text(encoding="utf-8"))
    if not order.get("acceptable"):                         # before Round 3: the week-07 buyer
        import market_buyer
        sys.argv = [sys.argv[0], "--shops", a.shops, "--item", a.item, "--budget", str(a.budget)]
        return market_buyer.main()
    order.setdefault("budget", a.budget)
    res = main_r3(a, order)
    (HERE / "logs").mkdir(exist_ok=True)
    with (HERE / "logs" / "buyer_r3.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "order": order.get("order"),
                            "item": order.get("item"), "budget": order.get("budget"),
                            "shop": res["shop"], "deal": bool(res["receipt"]),
                            "price": (res["receipt"] or {}).get("price"), "accept": res["accept"]},
                           ensure_ascii=False) + "\n")
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:                                   # noqa: BLE001 - always one result line
        log(f"[buyer] fatal {type(e).__name__}: {e}")
        print(json.dumps({"shop": "", "receipt": None, "stars": 3, "review": f"No trade ({type(e).__name__})."}))
        sys.exit(0)
