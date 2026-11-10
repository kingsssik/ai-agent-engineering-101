"""Week 08 -- forum posts: market reports anyone can check against the ledger.

Called by restock.py once a minute; posts at most every POST_EVERY as the buyer
(votes on buyer posts are 30 % of the buyer's rank) and every SHOP_EVERY as the
shop (what is in stock). A post is a line in .market/outbox.jsonl; the kit sends it.

The numbers come from market_memory (code); the harness only turns them into
two or three plain sentences. Each post cites ledger entries in `refs`, so a
reader -- or another agent -- can verify it, which is the point: a report that
checks out is worth a like, and one that does not is worth nothing.
"""
import json
import time
from pathlib import Path

import harness
import market_memory as mm

HERE = Path(__file__).resolve().parent
STATE = HERE / "memory" / "forum.json"
POST_EVERY = 5 * 60 + 20      # the kit allows one buyer post per 5 min; posts count 10 % of buyer ALL
SHOP_EVERY = 3 * 3600
MIN_SETTLES = 60               # say nothing until there is something to report


def leaf(c):
    return (c or "").split(">")[-1]


def facts(mem):
    hot = [(c, s) for c, s in mm.hot_categories(mem, 6) if s >= 1]
    rows = []
    for c, s in hot:
        crow = mem["cat"][c]
        rows.append({"category": leaf(c), "sales_per_day": round(s, 1),
                     "paid_share_of_list": round(mm.paid_ratio(mem, c, 0), 2) or None,
                     "ref": crow.get("last_seq")})
    shops = mem.get("shops", {})
    liars = sorted(((n, v["dishonest"], v["claims"]) for n, v in shops.items() if v.get("dishonest")),
                   key=lambda x: -x[1])[:3]
    grades = {g: f"{v['broken']}/{v['sold']}" for g, v in mem.get("grades", {}).items() if v.get("sold")}
    return {"hot": rows, "dishonest_claims": liars, "goods_broken_by_grade": grades,
            "settles_seen": mem.get("seen", {}).get("settle", 0)}


TOPICS = ("hot", "price", "trust")


def fallback_post(f, topic):
    """Code-written post, one topic per post, so consecutive posts are not repeats."""
    if topic == "price":
        rows = [r for r in f["hot"] if r.get("paid_share_of_list")]
        if rows:
            body = ", ".join(f"{r['category']} 정가의 {round(r['paid_share_of_list'] * 100)}%" for r in rows[:5])
            return f"최근 정산가 기준 실제 거래가: {body}. 정가보다 이만큼 낮게 사는 것이 보통입니다."
    if topic == "trust":
        parts = []
        if f["goods_broken_by_grade"]:
            parts.append("ledger 기준 비식품 고장/판매: "
                         + ", ".join(f"{g}등급 {v}" for g, v in sorted(f["goods_broken_by_grade"].items())))
        if f["dishonest_claims"]:
            parts.append("등급 주장이 정산 결과와 달랐던 가게(거짓/주장): "
                         + ", ".join(f"{n} {d}/{c}건" for n, d, c in f["dishonest_claims"]))
        if parts:
            return ". ".join(parts) + ". 증명서를 붙인 거래만 사는 것을 권합니다."
    hot = ", ".join(f"{r['category']} {r['sales_per_day']}/일" for r in f["hot"][:5])
    return f"최근 하루 정산 기준 잘 팔리는 카테고리: {hot}."


def write_post(f, topic="hot"):
    d = harness.ask(
        "Write a short market report for a forum where shopping agents trade (Korean, at most 3 sentences, "
        f"no greetings, no ads). Focus on the '{topic}' part of the data (hot = what sells, price = what share "
        "of list price deals close at, trust = broken goods by grade and dishonest grade claims). "
        "Use only these numbers, do not invent any:\n"
        + json.dumps(f, ensure_ascii=False) + '\nReply with one JSON object and nothing else: {"post": "<text>"}.',
        want=("post",), tag=" forum")
    text = str(d.get("post") or "").strip() or fallback_post(f, topic)
    return text[:700]


def outbox(state_dir, line):
    with (Path(state_dir) / "outbox.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def tick(state_dir, mem):
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {"buyer": 0, "shop": 0, "posts": []}
    now = time.time()
    if now - st["buyer"] > POST_EVERY and mem.get("seen", {}).get("settle", 0) >= MIN_SETTLES:
        f = facts(mem)
        if f["hot"]:
            topic = TOPICS[st.get("n", 0) % len(TOPICS)]
            st["n"] = st.get("n", 0) + 1
            text = write_post(f, topic)
            refs = [r["ref"] for r in f["hot"] if r.get("ref")][:5]
            outbox(state_dir, {"post": text, "refs": refs, "as": "buyer"})
            st["buyer"] = now
            st["posts"] = (st["posts"] + [{"ts": int(now), "as": "buyer", "text": text, "refs": refs}])[-200:]
    if now - st["shop"] > SHOP_EVERY:
        try:
            inv = json.loads((Path(state_dir) / "inventory.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            inv = {}
        cats = sorted({leaf(u.get("category")) for u in inv.get("units", []) if u.get("grade") != "expired"})
        if inv.get("round", 0) >= 3 and cats:
            text = ("YAHO1212 재고: " + ", ".join(cats[:25])
                    + ". 모든 상품 시장 증명서 첨부, 등급은 사실대로, 비식품 보증은 시장 고장률이 낮은 등급에만.")[:700]
            outbox(state_dir, {"post": text, "refs": [], "as": "shop"})
            st["shop"] = now
            st["posts"] = (st["posts"] + [{"ts": int(now), "as": "shop", "text": text}])[-200:]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
