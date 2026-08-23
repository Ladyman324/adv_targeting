"""Per-advisor product split of EIC's book, keyed on SEC advisor CRD.

WHAT THIS ANSWERS
-----------------
The advisor card shows one blended "assets with EIC" figure. The blend hides the
thing a rep is standing there to decide: WHICH product this relationship holds,
and therefore which one it does not. At firm level the asymmetry is stark --
Merrill Lynch holds $1.06B of All-Cap Value and exactly zero Large-Cap; Raymond
James is the mirror image. The same gaps exist advisor by advisor and nothing
surfaces them.

So this emits, per CRD: All-Cap Value SMA, Large-Cap Value SMA, and EICIX.

FULL ATTRIBUTION PER ADVISOR, DELIBERATELY
------------------------------------------
An account shared by four advisors is written at FULL value against all four.
That is right for a card -- the rep needs the size of the relationship they are
walking into, not a quarter of it -- and it is wrong for any total. 40% of
accounts have more than one holder, so summing this file across advisors
overstates the book by about 42%.

Rather than leave that as a trap, the file carries the authoritative
de-duplicated firm totals in its `totals` block. Nothing should ever recompute
them by summing `advisors`. audit.py enforces the distinction: the per-advisor
sum MUST exceed the stated total, and the stated total MUST match src/act_book.py.

CONFIDENCE IS CARRIED, NOT DISCARDED
------------------------------------
2,766 of the asset-holding contacts matched a CRD at high confidence and 645 at
review confidence. A review-tier match displaying real money against possibly
the wrong person is exactly the kind of quiet wrongness this project keeps
finding, so `t` carries the tier and the card is expected to qualify it.

Run:  python src/build_act_assets.py
"""
from __future__ import annotations

import collections
import datetime
import glob
import json
import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
from act_book import build, load, slots                  # noqa: E402

OUT = ROOT / "webapp" / "data" / "act_assets.json"
XW = ROOT / "data" / "interim" / "act_crosswalk.parquet"


def main() -> None:
    src, rows = load()
    accounts, conflicts, unresolved = build(rows)
    print(f"[*] {src.name}: {len(accounts):,} distinct accounts")

    if not XW.exists():
        raise SystemExit("No crosswalk. Run: python src/act_crosswalk.py")
    xw = pd.read_parquet(XW)
    crd_of = {r.act_id: (r.advisor_crd, r.tier) for r in xw.itertuples()
              if r.advisor_crd and r.tier in ("high", "review", "confirmed")}
    print(f"[*] crosswalk: {len(crd_of):,} Act! contacts carry a CRD")

    # Which Act! contacts hold each account.
    holders = collections.defaultdict(set)
    for r in rows:
        for code, *_ in slots(r.get("customFields") or {}):
            if code:
                holders[code].add(r.get("id"))

    # ACCOUNTS ARE EMITTED AS A TABLE, and advisors reference them by index.
    #
    # Without this a viewport summary is impossible to get right. 40% of
    # accounts have several holders, so adding up the visible advisors' totals
    # counts a team account once per member -- and the figure would then CHANGE
    # AS THE MAP PANS, because a second team-mate scrolling into view would add
    # their share again. Referencing accounts by index lets the client union the
    # set first and sum each account exactly once, at any zoom, under any filter.
    index_of = {code: i for i, code in enumerate(sorted(accounts))}
    table = [None] * len(index_of)
    for code, i in index_of.items():
        a = accounts[code]
        table[i] = [round(a["acv_sma"], 2), round(a["large"], 2), round(a["fund"], 2)]

    RANK = {"confirmed": 0, "high": 1, "review": 2}
    per = collections.defaultdict(lambda: {"acv": 0.0, "lcv": 0.0, "mf": 0.0,
                                           "n": 0, "shared": 0, "t": "review",
                                           "ix": []})
    placed_accounts = set()
    for code, a in accounts.items():
        for aid in holders[code]:
            hit = crd_of.get(aid)
            if not hit:
                continue
            crd, tier = hit
            e = per[crd]
            e["acv"] += a["acv_sma"]
            e["lcv"] += a["large"]
            e["mf"] += a["fund"]
            e["n"] += 1
            e["ix"].append(index_of[code])
            if a["holders"] > 1:
                e["shared"] += 1
            if RANK.get(tier, 9) < RANK.get(e["t"], 9):
                e["t"] = tier
            placed_accounts.add(code)

    # The authoritative totals: every account once, whether or not it reached a CRD.
    totals = {
        "acv": round(sum(a["acv_sma"] for a in accounts.values()), 2),
        "lcv": round(sum(a["large"] for a in accounts.values()), 2),
        "mf": round(sum(a["fund"] for a in accounts.values()), 2),
        "midcap": round(sum(a["midcap"] for a in accounts.values()), 2),
        "accounts": len(accounts),
        "accounts_on_map": len(placed_accounts),
        # The shape of the book, not just its size. These were computed here
        # already and printed to a terminal nobody keeps -- so the national view
        # had no way to say anything about EIC's own assets and showed three em
        # dashes, which reads as "we hold nothing" rather than "ask a smaller
        # question". Every figure below is de-duplicated: each account is
        # counted once, whoever holds it.
        "advisors": len(per),
        "advisors_review": sum(1 for e in per.values() if e["t"] == "review"),
        "shared_accounts": sum(1 for a in accounts.values() if a["holders"] > 1),
        # The asymmetry the whole file exists to surface, at national scale.
        "gap_acv": sum(1 for e in per.values() if e["acv"] > 0 and e["lcv"] == 0),
        "gap_lcv": sum(1 for e in per.values() if e["lcv"] > 0 and e["acv"] == 0),
        "both_smas": sum(1 for e in per.values() if e["acv"] > 0 and e["lcv"] > 0),
    }

    out = {}
    for crd, e in per.items():
        rec = {"t": e["t"], "n": e["n"], "ix": sorted(set(e["ix"]))}
        for k in ("acv", "lcv", "mf"):
            if e[k] > 0:
                rec[k] = round(e[k], 2)
        if e["shared"]:
            rec["sh"] = e["shared"]
        out[str(crd)] = rec

    payload = {
        "note": ("EIC assets by product, per advisor CRD. acv/lcv/mf are FULL "
                 "account values, so an account shared by several advisors appears "
                 "at full value on each -- correct for a card, WRONG for any sum. "
                 "To total across advisors, union their `ix` account indices and "
                 "sum `accounts` once per index. `totals` is the firm-wide figure."),
        "as_of": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": src.name,
        "columns": ["acv", "lcv", "mf"],
        "accounts": table,
        "totals": totals,
        "advisors": out,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")

    naive = sum(e["acv"] + e["lcv"] + e["mf"] for e in per.values())
    ded = totals["acv"] + totals["lcv"] + totals["mf"]
    tiers = collections.Counter(e["t"] for e in per.values())
    # Read back from `totals` rather than recomputed, so the console and the
    # national view cannot quote different numbers for the same question.
    gap_acv, gap_lcv = totals["gap_acv"], totals["gap_lcv"]

    print(f"[*] {len(out):,} advisors carry a book  ({dict(tiers)})")
    print(f"[*] accounts reaching an advisor on the map: "
          f"{totals['accounts_on_map']:,} of {totals['accounts']:,}")
    print(f"[*] per-advisor sum ${naive:,.0f} vs de-duplicated ${ded:,.0f} "
          f"({(naive-ded)/naive:.0%} overlap -- do not sum the advisors block)")
    print(f"[*] PRODUCT GAPS: {gap_acv:,} advisors hold All-Cap but no Large-Cap; "
          f"{gap_lcv:,} hold Large-Cap but no All-Cap")
    print(f"[*] wrote {OUT}")


if __name__ == "__main__":
    main()
