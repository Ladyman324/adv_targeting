"""EIC's book of business from the Act! CRM, de-duplicated, split by product and platform.

WHAT THE b* FIELDS ARE
----------------------
Act! carries up to eleven account slots per contact:

    bcode{n}     account number, prefixed with the platform  (MS, RJ, UB, ML...)
    bname{n}     the relationship name        e.g. "DEROSA/TOLMAN"
    bvalue_{n}   the account's total value
    ballcap_{n}  the All-Cap Value portion
    blarge_{n}   the Large-Cap Value portion
    bcount{n}    number of underlying accounts
    open_date_{n}

The PREFIX names the platform, with one exception that carries the product:
**a bcode beginning `MF` is the EICIX mutual fund**, and its value sits in the
all-cap column. Everything else is a separately managed account.

Mid-Cap Value has no column of its own. It appears as account value with neither
an All-Cap nor a Large-Cap allocation, so it is the residual.

DE-DUPLICATION IS THE WHOLE POINT
---------------------------------
An account is shared by everyone on the team that services it, and Act! repeats
the SAME bcode on each of their contact records. Summing per contact therefore
counts a team's assets once per member: 1,494 of 3,747 accounts have more than
one holder, up to eight, and the naive total overstates the book by **42%**
($14.8B against $8.5B).

This is the same trap `build_contacts.py` already documents for the CRM's single
"Total Assets" column -- 623 asset values shared across team members -- which is
why the map stores team assets on the TEAM and never on the person. The bcode is
what makes the correct version computable: it is an account identifier, so
counting each one once is simply right.

THREE NAMING ODDITIES, ALL REAL
-------------------------------
`bvalue_4` is spelled `bvalue4` (no underscore). `bvalue_5` is stored under
`basset5` -- its display name in Act! is "BValue 5". Slot 9's code is `bcode9`
with a lowercase c. Miss any of them and a slot silently reads as empty.

Run:  python src/act_book.py [--by-platform] [--csv]
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import json
import pathlib

ROOT = pathlib.Path(__file__).parents[1]
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "interim"

# prefix -> (group, label). THREE-CHARACTER PREFIXES ARE TESTED FIRST: `DMA` is
# a specific programme at Wells Fargo, and matching on two characters filed its
# 435 accounts and $634M under a phantom platform called "DM". Four codes do
# begin DM without being DMA (DMSA60, DMB26H, DMPTGO, DMPVDF) and are left
# unclassified rather than swept in.
#
# Unmapped prefixes are reported under their raw code, never lumped into
# "other" -- WF is a single account holding $79M, which is a question to ask
# somebody, not a rounding line. It is NOT assumed to be Wells Fargo on the
# strength of two letters.
PLATFORM = {
    "DMA": ("Wells Fargo", "Wells Fargo — DMA programme"),
    "MF":  ("EICIX", "EICIX mutual fund"),
    "MS":  ("Morgan Stanley", "Morgan Stanley"),
    "RJ":  ("Raymond James", "Raymond James"),
    "UB":  ("UBS", "UBS"),
    "WA":  ("Wells Fargo", "Wells Fargo / Wachovia"),
    "ML":  ("Merrill Lynch", "Merrill Lynch"),
    "EV":  ("Envestnet", "Envestnet"),
    "ST":  ("Stifel", "Stifel"),
    "PE":  ("Pershing", "Pershing"),
    "JA":  ("Janney", "Janney"),
    "US":  ("US Bank", "US Bank"),
    "LP":  ("LPL", "LPL"),
    "SW":  ("Schwab", "Schwab"),
}


def platform_of(code: str) -> tuple[str, str]:
    """(group, label) for an account code. Three characters before two."""
    for n in (3, 2):
        hit = PLATFORM.get(code[:n])
        if hit:
            return hit
    raw = code[:2] or "(blank)"
    return (raw, raw)


def num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def slots(cf: dict):
    """Yield (bcode, value, allcap, largecap) for every populated account slot."""
    for i in range(1, 12):
        code = cf.get(f"bcode{i}") if i != 9 else (cf.get("bcode9") or cf.get("bCode9"))
        val = cf.get("bvalue4") if i == 4 else cf.get(f"bvalue_{i}")
        if i == 5 and val is None:
            val = cf.get("basset5")
        if code is None and val is None and cf.get(f"bname{i}") is None:
            continue
        yield (str(code or "").strip().upper(), num(val),
               num(cf.get(f"ballcap_{i}")), num(cf.get(f"blarge_{i}")))


def load():
    files = sorted(glob.glob(str(RAW / "act_contacts_*.json")))
    if not files:
        raise SystemExit("No Act! pull found. Run act_client.py --census contacts --save")
    src = pathlib.Path(files[-1])
    return src, json.loads(src.read_text(encoding="utf-8"))


def build(rows):
    """De-duplicate accounts by bcode, keeping the most recently updated figure.

    WHICH COPY WINS. A team account appears on every member's record, and nine
    of them disagree about the amount -- one by $1.0M. First-seen was arbitrary,
    and arbitrary is not good enough for an asset figure.

    The order of preference is `bupdate` (the field Act! uses to stamp when the
    book data was last refreshed, which is precisely the question), then the
    contact's `edited` timestamp, then nothing. `edited` is weaker evidence --
    a record is edited for all sorts of reasons that have nothing to do with
    assets -- so it only breaks ties bupdate leaves. Anything still tied is
    REPORTED rather than silently resolved.
    """
    candidates = collections.defaultdict(list)
    holders = collections.defaultdict(set)
    for r in rows:
        cf = r.get("customFields") or {}
        stamp = (str(cf.get("bupdate") or ""), str(r.get("edited") or ""))
        for code, val, allc, lg in slots(cf):
            if not code:
                continue
            holders[code].add(r.get("id"))
            candidates[code].append((stamp, val, allc, lg, r.get("fullName")))

    accounts, conflicts, unresolved = {}, [], []
    for code, cands in candidates.items():
        distinct = {round(c[1], 2) for c in cands}
        # Newest first: bupdate, then edited. Empty strings sort last, which is
        # what we want -- a record with no stamp should not win.
        ranked = sorted(cands, key=lambda c: c[0], reverse=True)
        best = ranked[0]
        if len(distinct) > 1:
            conflicts.append((code, sorted(distinct)))
            if ranked[0][0] == ranked[1][0]:
                unresolved.append(code)
        _, val, allc, lg, _who = best
        group, label = platform_of(code)
        accounts[code] = {
            "code": code, "group": group, "label": label, "value": val,
            "allcap": allc, "large": lg,
            "fund": allc if code.startswith("MF") else 0.0,
            "acv_sma": 0.0 if code.startswith("MF") else allc,
            "midcap": max(0.0, val - allc - lg),
            "holders": len(holders[code]),
        }
    return accounts, conflicts, unresolved


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--by-platform", action="store_true", help="break the totals down by platform")
    ap.add_argument("--csv", action="store_true", help="write the account list to data/interim/")
    args = ap.parse_args()

    src, rows = load()
    accounts, conflicts, unresolved = build(rows)
    rowcount = sum(1 for r in rows for c, *_ in slots(r.get("customFields") or {}) if c)

    acv = sum(a["acv_sma"] for a in accounts.values())
    lcv = sum(a["large"] for a in accounts.values())
    fund = sum(a["fund"] for a in accounts.values())
    mid = sum(a["midcap"] for a in accounts.values())
    shared = sum(1 for a in accounts.values() if a["holders"] > 1)

    print(f"[*] {src.name}")
    print(f"[*] {rowcount:,} account rows across contacts -> {len(accounts):,} distinct accounts")
    print(f"[*] {shared:,} accounts ({shared/len(accounts):.0%}) are held by more than one advisor\n")

    print("BOOK OF BUSINESS, each account counted once")
    for lab, v in [("All-Cap Value SMA", acv), ("Large-Cap Value SMA", lcv),
                   ("EICIX mutual fund", fund), ("Mid-Cap / unallocated", mid)]:
        print(f"  {lab:<24} ${v:>16,.0f}")
    print(f"  {'TOTAL':<24} ${acv+lcv+fund+mid:>16,.0f}")

    if conflicts:
        print(f"\n[!] {len(conflicts)} accounts carry different values on different "
              f"contacts. The most recently updated copy was used (bupdate, then edited):")
        for code, vals in conflicts:
            mark = "  UNRESOLVED — identical timestamps" if code in unresolved else ""
            print(f"      {code:<12} " + " / ".join(f"${v:,.0f}" for v in vals) + mark)

    if args.by_platform:
        # Group roll-up first: DMA is a Wells Fargo programme, so the firm's
        # real exposure to Wells is the two lines added together, which is not
        # visible when they sit apart in an alphabetical table.
        grp = collections.defaultdict(lambda: [0.0, 0])
        for a in accounts.values():
            g = grp[a["group"]]
            g[0] += a["acv_sma"] + a["large"] + a["fund"] + a["midcap"]
            g[1] += 1
        print(f"\nBY FIRM{'':<24}{'accts':>7}{'total':>16}")
        for k, v in sorted(grp.items(), key=lambda kv: -kv[1][0])[:10]:
            print(f"  {k:<28}{v[1]:>7,}{v[0]:>16,.0f}")

        by = collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0])
        for a in accounts.values():
            b = by[a["label"]]
            b[0] += a["acv_sma"]; b[1] += a["large"]; b[2] += a["fund"]
            b[3] += a["midcap"]; b[4] += 1
        print(f"\n{'platform':<26}{'accts':>7}{'All-Cap':>16}{'Large-Cap':>16}"
              f"{'Fund':>14}{'Mid/other':>12}{'total':>16}")
        for k, v in sorted(by.items(), key=lambda kv: -sum(kv[1][:4])):
            if sum(v[:4]) <= 0:
                continue
            print(f"  {k:<28}{v[4]:>7,}{v[0]:>16,.0f}"
                  f"{v[1]:>16,.0f}{v[2]:>14,.0f}{v[3]:>12,.0f}{sum(v[:4]):>16,.0f}")

    if args.csv:
        OUT.mkdir(parents=True, exist_ok=True)
        p = OUT / "act_book_accounts.csv"
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["bcode", "group", "platform", "holders", "value",
                        "acv_sma", "large", "fund", "midcap"])
            for a in sorted(accounts.values(), key=lambda a: -a["value"]):
                w.writerow([a["code"], a["group"], a["label"], a["holders"],
                            round(a["value"], 2), round(a["acv_sma"], 2), round(a["large"], 2),
                            round(a["fund"], 2), round(a["midcap"], 2)])
        print(f"\n[*] wrote {p}  ({len(accounts):,} accounts) — local only, data/ is gitignored")


if __name__ == "__main__":
    main()
