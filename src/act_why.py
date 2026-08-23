"""Why did this advisor's call not reach Act! -- answered from the crosswalk.

`actStatus: no-contact` on a logged call means the CRD resolved to no Act!
contact. That is a deliberate refusal rather than a failure: only high-confidence
matches sync, because writing a call onto a contact we are not sure of files a
real conversation against a stranger, unfindably.

But "we refused" is not an answer anyone can act on. There are four different
reasons a CRD is absent and they need different responses:

  no crosswalk row   the advisor is not in Act! at all -> a prospect, nothing to fix
  review tier        we have a candidate and are not sure -> confirm it by hand
  none tier          scored too low to be a candidate    -> probably genuinely absent
  ambiguous          the CRD matched two Act! contacts   -> merge or pick, in Act!

Run:  python src/act_why.py --crd 1234567
      python src/act_why.py --crd 1234567 --name "Jane Smith"
      python src/act_why.py --unmatched-top 20      # where the gap is worst
"""
from __future__ import annotations

import argparse
import json
import pathlib

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
XWALK = ROOT / "data" / "interim" / "act_crosswalk.parquet"
LOOKUP = ROOT / "api" / "shared" / "act_contacts.json"
CONTACTS = ROOT / "webapp" / "data" / "contacts.json"


def load():
    df = pd.read_parquet(XWALK)
    df["advisor_crd"] = df.advisor_crd.astype(str)
    shipped = json.loads(LOOKUP.read_text(encoding="utf-8"))["contacts"]
    return df, shipped


def staff_match(crd: str):
    """The Act! staff record for this advisor, if that is why they are absent.

    Matched on EMAIL, taken from the map's own contact record, because it is the
    one identifier both sides agree on. Name matching would reintroduce exactly
    the ambiguity this whole crosswalk exists to avoid.
    """
    import glob                                            # noqa: PLC0415
    files = sorted(glob.glob(str(ROOT / "data" / "raw" / "act_contacts_*.json")))
    if not files:
        return None
    try:
        adv = json.loads(CONTACTS.read_text(encoding="utf-8"))["advisors"].get(str(crd))
    except FileNotFoundError:
        return None
    email = str((adv or {}).get("e") or "").strip().lower()
    if not email:
        return None
    for r in json.loads(pathlib.Path(files[-1]).read_text(encoding="utf-8")):
        if not r.get("isUser"):
            continue
        if str(r.get("emailAddress") or "").strip().lower() == email:
            return {"id": r.get("id"),
                    "name": r.get("fullName") or r.get("displayName") or ""}
    return None


def explain(crd: str) -> None:
    df, shipped = load()
    crd = str(crd).strip()
    rows = df[df.advisor_crd == crd]

    print(f"CRD {crd}\n")

    # HOW OLD IS THE EVIDENCE. Every "they should be in Act!, why aren't they"
    # question has the same first answer available and it was not being shown:
    # the crosswalk is built from a point-in-time pull, so anyone added to the
    # CRM since then is invisible to it and looks identical to someone who was
    # never there. Printed before the verdict, because it qualifies the verdict.
    import glob                                            # noqa: PLC0415
    import datetime                                        # noqa: PLC0415
    pulls = sorted(glob.glob(str(ROOT / "data" / "raw" / "act_contacts_*.json")))
    if pulls:
        stamp = pathlib.Path(pulls[-1]).stem.replace("act_contacts_", "")
        try:
            age = (datetime.date.today()
                   - datetime.date.fromisoformat(stamp)).days
            warn = "   <-- anyone added to Act! since then is invisible here" \
                if age >= 1 else ""
            print(f"   Act! pull      : {stamp}  ({age} day"
                  f"{'' if age == 1 else 's'} old){warn}")
        except ValueError:
            print(f"   Act! pull      : {stamp}")

    # Who the map thinks this is, if we can say. Named before the verdict so the
    # answer is checkable rather than merely stated.
    try:
        adv = json.loads(CONTACTS.read_text(encoding="utf-8"))["advisors"].get(crd)
        if adv:
            print(f"   in the map as : {adv.get('n', '(no name)')}"
                  f"{'  ' + adv['cn'] if adv.get('cn') else ''}")
        else:
            print("   in the map as : NOT PRESENT — this CRD is not an advisor in "
                  "contacts.json, so no card in the app points at it")
    except FileNotFoundError:
        pass

    if crd in shipped:
        print(f"\n   VERDICT: syncable. Act! contact {shipped[crd]}")
        print("   A call logged against this advisor should reach Act!. If one did")
        print("   not, the cause is downstream — check /api/health.")
        return

    if rows.empty:
        # BEFORE concluding "not in the CRM", check whether they were EXCLUDED
        # from it. The crosswalk drops the 26 Act! user records as staff, and an
        # absent row looks identical either way -- so this tool once told a
        # colleague who is in Act!, in the map and in the SEC feed that he was
        # "genuinely not in the CRM". A confidently wrong diagnostic is worse
        # than no diagnostic; the reader stops looking.
        staff = staff_match(crd)
        if staff:
            print("\n   VERDICT: excluded as staff.")
            print(f"      Act! contact : {staff['name']}  {staff['id']}")
            print("   This person IS in Act!, and their contact record carries")
            print("   isUser=true. src/act_crosswalk.py drops all 26 Act! user")
            print("   records on the rule that users are staff rather than")
            print("   prospects, so no crosswalk row is ever built for them.")
            print("\n   That rule is right, and it means EIC's own advisors cannot")
            print("   be sync targets. Use a real external advisor to test.")
            return
        print("\n   VERDICT: no crosswalk row at all.")
        print("   Nobody in Act! was matched to this CRD, at any confidence, and")
        print("   they are not one of the excluded staff records. Most likely")
        print("   genuinely absent from the CRM — a prospect rather than a gap.")
        print("   Adding them to Act! and rebuilding the crosswalk is what would")
        print("   change it.")
        return

    if len(rows) > 1:
        print(f"\n   VERDICT: ambiguous — {len(rows)} Act! contacts matched this CRD.")
        print("   Deliberately excluded: choosing between them is a coin flip, and")
        print("   the losing side is a real call filed onto the wrong person.")
        for r in rows.itertuples():
            print(f"      {r.act_id}  {r.name:<28} {str(r.company)[:26]:<26} "
                  f"tier={r.tier} score={r.match_score}")
        print("\n   Fix in Act!: merge the duplicates, or delete the stale one.")
        return

    r = rows.iloc[0]
    print(f"\n   VERDICT: matched at tier '{r.tier}', which does not sync.")
    print(f"      candidate : {r['name']}  {str(r.company)[:40]}")
    print(f"      score     : {r.match_score}   gap to next {r.match_gap}"
          f"   namesakes {r.namesakes}")
    print(f"      act id    : {r.act_id}")
    if r.tier == "review":
        print("\n   We have a plausible candidate and are not certain enough to")
        print("   write a call onto them. Confirm by hand and the sync follows.")
    else:
        print("\n   Scored too low to be a candidate. Probably genuinely absent")
        print("   from Act!.")


def unmatched_top(n: int) -> None:
    """Where the gap actually costs something -- ranked by EIC assets held.

    A flat count of 20,921 unmatched advisors is not actionable. The ones worth
    confirming by hand are the ones we already do business with.
    """
    df, shipped = load()
    try:
        book = json.loads((ROOT / "webapp" / "data" / "act_assets.json")
                          .read_text(encoding="utf-8"))
    except FileNotFoundError:
        print("[!] act_assets.json not built; cannot rank by assets.")
        return

    accounts = book["accounts"]
    rows = []
    for crd, a in book["advisors"].items():
        if crd in shipped:
            continue
        total = sum(sum(accounts[i]) for i in a.get("ix", []))
        rows.append((total, crd, a.get("n", ""), a.get("t", "")))
    rows.sort(reverse=True)

    print(f"{len(rows):,} advisors hold EIC assets and cannot sync to Act!.")
    print(f"Top {n} by assets — these are the ones worth confirming by hand:\n")
    print(f"   {'assets':>12}  {'crd':<10} {'tier':<8} name")
    for total, crd, name, tier in rows[:n]:
        print(f"   {'$' + format(round(total), ','):>12}  {crd:<10} {tier:<8} {name}")
    print(f"\n   total behind the gap: "
          f"${sum(r[0] for r in rows):,.0f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--crd", help="the advisor CRD from the actStatus row")
    ap.add_argument("--unmatched-top", type=int, metavar="N",
                    help="the N unsyncable advisors holding the most EIC assets")
    args = ap.parse_args()
    if args.crd:
        explain(args.crd)
    elif args.unmatched_top:
        unmatched_top(args.unmatched_top)
    else:
        ap.error("pass --crd or --unmatched-top")


if __name__ == "__main__":
    main()
