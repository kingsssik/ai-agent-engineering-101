"""Week 08 -- the seller's price tool: floor, opening ask and terms for one unit.

The harness negotiates, but between two numbers this module sets in code:

  floor   the lowest price the shop may accept for this unit right now. It is
          the unit's cost plus a margin while the unit is fresh, and it falls
          as the unit loses value to the shop: food close to its date (thrown
          away at cost when it expires), and everything in the last hours of
          the round (unsold stock counts as a loss of its whole cost). Expected
          warranty refunds are added on top.
  ask     where to open: the list price, pulled toward what buyers in this
          category have actually paid (market_memory), never below the floor.
  terms   the claim (the true grade, always), the certificate (always shown)
          and a warranty for goods when the market's break rate for that grade
          is low enough to be worth carrying.
"""
import os
import time
from datetime import datetime, timezone

import market_memory as mm

ROUND_END = float(os.environ.get("ROUND_END", datetime(2026, 11, 17, 14, 59, tzinfo=timezone.utc).timestamp()))
MARGIN = 1.04                  # fresh unit: at least cost + 4 % (11-10: was 1.12; grade A costs ~0.66 of list,
                               # so its floor sat at 0.74, above what buyers pay -- 61 deals from 715 openings)
ASK_CAP = {"A": 0.70, "B": 0.70}   # opening ask / list. Deals per opening, 11-08..11-10: A 35 % at 0.70,
                               # 12 % at 0.75, 2 % at 0.85; B 48 % at 0.70, 25 % at 0.75. C converts ~25 % at
                               # any ask from 0.65 to 0.80, so C keeps the higher ask.
WARRANTY_HOURS = 6             # goods break up to 6 h after the sale
WARRANTY_MAX_RATE = 0.12       # offer a warranty only below this break rate


def hours_left(unit, now=None):
    exp = unit.get("expires_at")
    return None if not exp else (exp - (now or time.time())) / 3600


def floor(unit, mem, now=None):
    now = now or time.time()
    cost = unit.get("cost") or 0
    f = cost * MARGIN
    h = hours_left(unit, now)
    if h is not None:                                      # food: what is it worth if it does not sell soon?
        if h < 6:
            f = cost * 0.3
        elif h < 24:
            f = cost * 0.7
        elif str(unit.get("grade")) == "C":
            f = cost * 1.0
    to_end = (mm_round_end() - now) / 3600
    if to_end < 12:
        f = min(f, cost * 0.4)                             # unsold at the end is the whole cost lost
    elif to_end < 36:
        f = min(f, cost * 0.85)
    if warranty(unit, mem):
        f += mm.break_rate(mem, unit.get("grade")) * unit.get("list_price", 0) * 0.9
    return max(1, int(round(f)))


def mm_round_end():
    return ROUND_END


def ask(unit, mem, now=None):
    lp = unit.get("list_price") or 0
    ratio = mm.paid_ratio(mem, unit.get("category"), default=0.9)
    a = lp * min(1.0, ratio + 0.08, ASK_CAP.get(str(unit.get("grade")), 1.0))
    return max(floor(unit, mem, now), int(round(a)))


def warranty(unit, mem):
    if unit.get("kind") != "goods" or str(unit.get("grade")) == "C":
        return 0
    return WARRANTY_HOURS if mm.break_rate(mem, unit.get("grade")) <= WARRANTY_MAX_RATE else 0


def describe(unit, now=None):
    """What the shop tells the buyer about the unit: true, and checkable against the certificate."""
    g = str(unit.get("grade"))
    if unit.get("kind") == "food":
        h = hours_left(unit, now)
        return f"grade {g}, {h:.0f} h of shelf life left, market certificate attached" if h is not None \
            else f"grade {g}, certificate attached"
    words = {"A": "new (A)", "B": "box damaged (B)", "C": "refurbished (C)"}.get(g, f"grade {g}")
    return f"{words}, market certificate attached"
