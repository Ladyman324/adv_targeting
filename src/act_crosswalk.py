"""Act! contact GUID <-> SEC advisor CRD, persisted so it stops being recomputed and thrown away.

WHY THIS EXISTS
---------------
`build_contacts.score_contacts()` already decides which advisor each CRM record
is, every single run, with a tier and a score -- and then keeps only the merged
output and discards the mapping. Everything that wants to write back to Act!
needs exactly that discarded fact: to post a call outcome against an advisor, a
contact GUID has to be known.

THE ID PROBLEM IT SOLVES
------------------------
The Excel export has no stable key. Every rebuild re-matched all 47,426 rows
from scratch, and there was no way to say "this is the same person we matched
last quarter". The API pull carries `id`, an Act! GUID, so a crosswalk built
from the API is durable in a way one built from the spreadsheet could never be.

That is why this reads `data/raw/act_contacts_*.json` and not the Excel.

IT REUSES THE MATCHER, IT DOES NOT REIMPLEMENT IT
-------------------------------------------------
`score_contacts()` is imported and called. A second matcher tuned separately
would drift from the one that builds the map, and the two would disagree about
who somebody is -- silently, since both would look reasonable. There is one
matcher in this project and this file is a caller of it.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It writes nothing to Act!. It produces a local file. Pushing CRDs into a custom
field, or posting history, are separate acts that read this.

Run:  python src/act_crosswalk.py [--report]
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from build_contacts import (clean_email, derive_domain_map, load_index,   # noqa: E402
                            score_contacts, strip_designations)
from nicknames import any_name_agrees, same_person                       # noqa: E402

RAW = ROOT / "data" / "raw"
INTERIM = ROOT / "data" / "interim"
OUT = INTERIM / "act_crosswalk.parquet"


def newest_pull() -> pathlib.Path:
    files = sorted(glob.glob(str(RAW / "act_contacts_*.json")))
    if not files:
        raise SystemExit(
            "No Act! pull found. Run:\n"
            "  python src/act_client.py --user <you> --db EQUITYINVESTMENT "
            "--census contacts --save")
    return pathlib.Path(files[-1])


def to_frame(rows: list[dict], domains: dict) -> pd.DataFrame:
    """Act! records in the column shape score_contacts() expects.

    Only the fields the matcher reads. Everything else about a contact is the
    build's business, not the crosswalk's.
    """
    out = []
    for r in rows:
        cf = r.get("customFields") or {}
        addr = r.get("businessAddress") or {}
        email = clean_email(r.get("emailAddress") or "")
        name = strip_designations(
            f"{r.get('firstName') or ''} {r.get('lastName') or ''}".strip())
        out.append({
            "act_id": str(r.get("id") or ""),
            "name": name,
            "email": email,
            # Same rule the CRM loader uses: the email domain names the firm.
            "firm_crd": domains.get(email.split("@")[-1], "") if email else "",
            "city": str(addr.get("city") or "").strip(),
            "state": str(addr.get("state") or "").strip().upper(),
            "company": str(r.get("company") or "").strip(),
            # Act! holds no CRD -- confirmed across all 190 field definitions --
            # so the matcher never gets a free answer here. Declared anyway
            # because score_contacts() reads it, and because the day a CRD field
            # IS populated this file starts using it with no further change.
            "given_crd": str(cf.get("sec_crd") or cf.get("crd") or "").strip(),
            # Carried for the report only.
            "is_user": bool(r.get("isUser")),
            "tier_abc": str(cf.get("tier__a_b_c") or "").strip(),
        })
    return pd.DataFrame(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--report", action="store_true",
                    help="print the distribution and stop; write nothing")
    args = ap.parse_args()

    src = newest_pull()
    rows = json.loads(src.read_text(encoding="utf-8"))
    print(f"[*] {src.name}: {len(rows):,} Act! contacts")

    domains = derive_domain_map()
    people = to_frame(rows, domains)

    # STAFF ARE NOT EXCLUDED. They used to be -- `people[~people.is_user]`, on
    # the rule that Act! users are colleagues rather than prospects. That sounds
    # obviously right and is wrong here.
    #
    # EIC's own advisers are SEC-registered and appear in the map like anyone
    # else, so the rule made 26 people permanently unreachable: present in Act!,
    # present in the map, and silently unable to carry a logged call. Found when
    # a call logged against a colleague came back `no-contact` and every obvious
    # explanation -- wrong email, missing CRD, broken lookup -- was wrong.
    # Anyone with a CRD should be contactable.
    #
    # `is_user` is still carried: "this contact is a colleague" is worth being
    # able to see. It just no longer decides anything.
    staff = int(people["is_user"].sum())
    print(f"[*] {staff} Act! user records INCLUDED — staff are matched like "
          f"anyone else, because they hold CRDs too")

    index = load_index()
    scored = score_contacts(people, index)

    # DEMOTE ANY HIGH-TIER MATCH WHOSE FIRST NAMES DISAGREE.
    #
    # The matcher scores on surname, firm and location, so two people who share
    # a surname at the same office score identically -- and 862 high-tier pairs
    # turned out to disagree on the first name. Not nicknames: Jeffrey and
    # Victoria Thompson, Raymond and Rosemary Abreu, Terri and Darren Hunter.
    # 340 of them were syncable, which means a logged call would have been
    # written onto the wrong person's Act! record: correctly attributed,
    # entirely plausible, and unfindable afterwards.
    #
    # The first name was sitting in both files the whole time and nothing
    # compared them. Demoting to `review` rather than dropping the row keeps the
    # evidence and reuses the meaning the tier already has -- "plausible, not
    # certain" -- which is exactly the state these are in. `review` does not
    # sync, so the harm stops without a new concept.
    #
    # src/nicknames.py decides what "disagree" means; without it this would
    # demote several hundred CORRECT matches (Jim/James, Bob/Robert) and quietly
    # shrink the sync instead of making it safer.
    # The index holds each advisor's SEC given-name TOKENS, which is better than
    # one name string: "Robert Nelson Murray Jr" carries both `robert` and
    # `nelson`, so an Act! record reading "Nelson Murray" agrees with the SEC
    # rather than looking like a stranger. Agreement with ANY token is enough.
    sec_tokens: dict[str, set[str]] = {}
    for recs in index.values():
        for crd, firsts, _meta in recs:
            sec_tokens.setdefault(str(crd), set()).update(firsts)

    # The index does not cover every advisor in the map, and an empty token set
    # used to mean "nothing to disagree with" -- which let 599 genuine mismatches
    # straight through the gate, Mitchell vs Kristin Stillman among them. A
    # permissive default on the ONE check standing between a logged call and the
    # wrong person's record is the wrong default.
    #
    # So the map's own display name is the fallback. It is the identity the rep
    # sees on the card, which makes it the right thing for an Act! record to have
    # to agree with, and it is what audit.py checks -- gate and check now ask the
    # same question of the same data.
    map_names = {}
    try:
        map_names = {k: (v.get("n") or "") for k, v in json.loads(
            (ROOT / "webapp" / "data" / "contacts.json")
            .read_text(encoding="utf-8"))["advisors"].items()}
    except FileNotFoundError:
        print("[!] contacts.json not built — first-name demotion falls back to "
              "the SEC index alone and will be less complete")

    def agrees(act_name: str, crd: str) -> bool:
        """The map's displayed name is AUTHORITATIVE where we have one.

        This used to be an OR -- agree with the SEC index tokens *or* with the
        map name -- and an OR on a safety gate is a way of saying "any one
        witness will do". An index token then rescued 87 pairs the displayed
        name plainly rejected: John vs Andrew Davis, Mark vs David Evans.

        The name on the card is the identity the rep is looking at when they log
        the call, so it is the one an Act! record has to agree with. Index tokens
        only decide the advisors the map has no name for.
        """
        crd = str(crd)
        mapped = map_names.get(crd)
        if mapped:
            return any_name_agrees(act_name, mapped)
        toks = sec_tokens.get(crd)
        if not toks:
            return True                  # no evidence either way: do not demote
        return any(same_person(act_name, t) for t in toks)

    hi = scored["tier"] == "high"
    disagree = hi & ~pd.Series(
        [agrees(n, c) for n, c in zip(scored["name"].fillna(""),
                                      scored["advisor_crd"].fillna(""))],
        index=scored.index)
    n_dem = int(disagree.sum())
    scored.loc[disagree, "tier"] = "review"
    scored["demoted"] = ""
    scored.loc[disagree, "demoted"] = "first name disagrees with the SEC record"

    dist = scored["tier"].value_counts()
    print(f"[*] {n_dem:,} high-tier matches demoted to review — the Act! first "
          f"name disagrees with the SEC record, so they are probably different "
          f"people who share a surname")
    print("[*] tiers: " + ", ".join(f"{k} {v:,}" for k, v in dist.items()))

    matched = scored[(scored["advisor_crd"] != "")].copy()
    # One CRD can attract several Act! records -- a person entered twice, or a
    # shared team mailbox. Kept rather than collapsed: which duplicate is
    # canonical is a judgement, and this file records evidence, not judgements.
    dupe_crd = int(matched["advisor_crd"].duplicated().sum())
    dupe_act = int(matched["act_id"].duplicated().sum())
    print(f"[*] {len(matched):,} matched rows; {dupe_crd:,} share a CRD with "
          f"another row; {dupe_act:,} duplicate act_ids (should be 0)")

    if args.report:
        print(scored.groupby(["tier"]).size().to_string())
        high = scored[scored["tier"] == "high"]
        print(f"\nhigh-tier score: min {high['match_score'].min():.3f}, "
              f"median {high['match_score'].median():.3f}")
        return

    # `demoted` is carried into the file so the 721 first-name demotions can be
    # reviewed later. A row that says only "review" loses the reason it is there,
    # and these are the rows most worth a human eye: each is either a namesake we
    # correctly refused, or a real match we can restore by hand.
    keep = scored[["act_id", "advisor_crd", "tier", "match_score", "match_gap",
                   "namesakes", "name", "email", "state", "company", "tier_abc",
                   "demoted"]].copy()
    keep["built_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    keep["source_file"] = src.name
    INTERIM.mkdir(parents=True, exist_ok=True)

    # Compare against the previous crosswalk BEFORE overwriting it. A matcher
    # change that silently moves 400 people to different advisors is the exact
    # failure this project keeps hitting, and the only moment it is visible is
    # right here, with both versions in hand.
    if OUT.exists():
        prev = pd.read_parquet(OUT)
        a = prev[prev.tier == "high"].set_index("act_id")["advisor_crd"]
        b = keep[keep.tier == "high"].set_index("act_id")["advisor_crd"]
        both = a.index.intersection(b.index)
        moved = int((a.loc[both] != b.loc[both]).sum())
        print(f"[*] vs previous crosswalk: {len(both):,} high-tier ids in both, "
              f"{moved:,} now point at a DIFFERENT advisor"
              f"{'  <-- investigate' if moved else ''}")
        print(f"    high-tier count {len(a):,} -> {len(b):,}")

    keep.to_parquet(OUT, index=False)
    print(f"[*] wrote {len(keep):,} rows to {OUT}")


if __name__ == "__main__":
    main()
