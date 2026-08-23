"""act_crosswalk.parquet -> api/shared/act_contacts.json, so the API can resolve
a CRD to an Act! contact id at the moment a rep logs a call.

WHY A FILE SHIPPED WITH THE API
-------------------------------
The crosswalk lives in data/interim as parquet, which the Node functions cannot
read and which is not deployed. The alternatives were a Table Storage table (one
more thing to keep in sync, and a network round trip on every log) or this: a
plain object loaded once per cold start. It is a derived artifact rebuilt from
the same command that builds the crosswalk, so there is no second source of
truth -- only a second FORMAT, which audit.py checks agrees with the first.

HIGH TIER ONLY, AND THAT IS THE POINT
-------------------------------------
The crosswalk has three tiers: high (27,836), review (13,738) and none (5,866).
Only `high` goes in here. A review-tier match is a guess with a decent score,
and writing a call into the CRM against a guess means writing it onto SOMEBODY
ELSE'S contact record -- a stranger's history showing a call that never happened
to them. There is no error message for that and no way to find it later.

So a review-tier advisor logs locally and does not sync, and the rep is told
nothing, because there is nothing they could do about it. The gap is reported
here at build time instead, where it can be worked on in bulk.

Run:  python src/build_act_lookup.py
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
# The same nickname table the matcher and the audit use. Bill IS William here,
# so only a genuine disagreement drops a pair.
from nicknames import any_name_agrees

ROOT = pathlib.Path(__file__).parents[1]

# How clear a win has to be before a first-name disagreement is forgiven as an
# informal name rather than treated as the wrong person. Measured: the informal
# names cluster at 0.49 and above, the wrong people at 0.27 and below.
NAME_DISAGREEMENT_GAP = 0.30
SRC = ROOT / "data" / "interim" / "act_crosswalk.parquet"
OUT = ROOT / "api" / "shared" / "act_contacts.json"
CONTACTS = ROOT / "webapp" / "data" / "contacts.json"


def given_names_disagree(sec_name: str, sec_email: str, act_email: str) -> bool:
    """Do the two addresses name DIFFERENT people, surname set aside?

    Only ever consulted when the names already disagree, so this is the second
    of two independent signals rather than a test on its own.

    Returns False whenever it cannot tell -- one side missing an address, or a
    local part with nothing in it but the surname. An absent answer must never
    read as a positive one: this decides whether somebody's CRM record is
    written to, and "no evidence" is not "evidence of wrongness".
    """
    surname = re.sub(r"[^a-z]", "", str(sec_name or "").split()[-1].lower()) if sec_name else ""

    def given(email: str) -> set:
        text = str(email or "").strip().lower()
        if "@" not in text:
            return set()
        out = set()
        for part in re.split(r"[._\-0-9]+", text.split("@")[0]):
            part = re.sub(r"[^a-z]", "", part)
            # Single letters are initials and agree with anything -- `a.rollins`
            # is not evidence against Arthur OR against April.
            if len(part) > 1 and part != surname:
                out.add(part)
        return out

    ours, theirs = given(sec_email), given(act_email)
    if not ours or not theirs:
        return False
    return not (ours & theirs)


def sec_email_map() -> dict:
    """CRD -> the address the FIRM publishes, from the built contact file.

    Optional by design. This runs in a pipeline where contacts.json may not have
    been rebuilt yet, and a missing file simply means the email signal is
    unavailable and the margin rule stands alone -- which is the behaviour that
    existed before this was added.
    """
    if not CONTACTS.exists():
        return {}
    try:
        advisors = json.loads(CONTACTS.read_text(encoding="utf-8"))["advisors"]
    except (ValueError, KeyError):
        return {}
    return {str(crd): str(rec.get("e", "") or "") for crd, rec in advisors.items()}


def main() -> None:
    df = pd.read_parquet(SRC)
    total = len(df)
    high = df[df.tier == "high"].copy()

    # A CRD pointing at two Act! contacts is not resolvable, and picking one is
    # how a call lands on the wrong record. Drop both and say so.
    dupes = high[high.duplicated("advisor_crd", keep=False)]
    if len(dupes):
        keep_crds = set(high.advisor_crd) - set(dupes.advisor_crd)
        high = high[high.advisor_crd.isin(keep_crds)]

    # A NAMESAKE IS NOT A MATCH.
    #
    # The crosswalk scores on surname, firm and location, so two people called
    # Cahill at Merrill in Florida score almost identically -- and 17 CRDs won
    # by margins of 0.12 to 0.15 against 2 or 3 namesakes. CRD 1213372 is
    # PATRICK MICHAEL CAHILL in the SEC feed and Edward Cahill in Act!, and a
    # call logged against it would be written onto Edward's contact: correctly
    # attributed, entirely plausible, and unfindable afterwards.
    #
    # The first name sits in both files and nothing compared them. Compared here,
    # against the SEC record rather than against the map, because the SEC record
    # is what the CRD means. Nicknames are allowed -- Bill for William, Jeff for
    # Jeffrey -- so only a genuine disagreement drops the pair.
    #
    # DROPPED, not demoted to review: the whole value of this file is that a CRD
    # in it can be written to without further thought. A pair we cannot tell
    # apart is worth less than no pair at all.
    sec = pd.read_parquet(
        ROOT / "data" / "output" / "advisors.parquet",
        columns=["advisor_crd", "first_name", "middle_name", "last_name"],
    ).drop_duplicates("advisor_crd")
    sec["advisor_crd"] = sec["advisor_crd"].astype(str)
    filed = {r.advisor_crd: " ".join(
        str(x) for x in (r.first_name, r.middle_name, r.last_name)
        if x and str(x) != "None") for r in sec.itertuples()}

    sec_emails = sec_email_map()

    mismatched = []
    for row in high.itertuples():
        crd, act_name = str(row.advisor_crd), str(row.name or "")
        sec_name = filed.get(crd, "")
        if not (sec_name and act_name) or any_name_agrees(act_name, sec_name):
            continue
        # A DISAGREEMENT ALONE IS NOT ENOUGH, and dropping on it costs real
        # people. 536 high-tier pairs disagree, and most are somebody using a
        # name the nickname table has never heard of: JP for Jean Paul, Chip for
        # Frank Edward, Rus for G. Cyrus, Bo for Edward Byron. Those are the
        # right contacts, informally named.
        #
        # What separates them is the MARGIN THE MATCH WON BY. An informal name
        # sits on a match nothing else came close to -- gap 0.82, 0.90, 1.00.
        # A wrong person sits on a match that barely beat a runner-up -- 0.12,
        # 0.15, 0.25 -- because the runner-up was the actual advisor, scoring
        # almost identically on the surname, firm and city they share.
        #
        # So the rule is "the name disagrees AND the matcher was not sure", not
        # "the name disagrees". 247 pairs rather than 536, and Shanicia Harris
        # stops being Dave Harris while Chip Messenger stays Chip.
        # EMAIL OVERRIDES THE MARGIN, because it is evidence the matcher
        # never saw.
        #
        # The gap rule forgives a confident match on the assumption that a
        # disagreeing name is an informal one. That assumption is WRONG when
        # both records carry an address and the given names in them disagree:
        #
        #     art_rollins@ml.com     vs  lorin.rollins@ml.com
        #     janice.cope@ml.com     vs  brent_cope@ml.com
        #     jed.dolce@ubs.com      vs  donn.dolce@ubs.com
        #     ashley_brunson@ml.com  vs  april_brunson@ml.com
        #
        # Every one of those won its match by a WIDE margin -- 0.52 to 0.82 --
        # because the runner-up was the actual advisor, scoring almost
        # identically on the surname, firm and city they share. The margin
        # cannot separate them. The address can, and the firm wrote it.
        #
        # The SURNAME is excluded before comparing, or every one of these looks
        # like agreement: `rollins` appears on both sides. Only the given-name
        # part carries the distinction.
        #
        # Deliberately NOT "drop when the emails disagree". 32 pairs do, and 27
        # of them are one person with two address formats -- gregory.delmonte
        # and gdelmonte, bob.robinson and rrobinson. Those names AGREE, so they
        # never reach this line. It takes both signals failing.
        if (float(row.match_gap or 0) >= NAME_DISAGREEMENT_GAP
                and not given_names_disagree(sec_name, sec_emails.get(crd, ""), row.email)):
            continue
        mismatched.append((crd, sec_name, act_name, int(row.namesakes or 0)))
    if mismatched:
        drop = {crd for crd, *_ in mismatched}
        high = high[~high.advisor_crd.astype(str).isin(drop)]

    m = {str(r.advisor_crd): str(r.act_id) for r in high.itertuples()}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "note": ("CRD -> Act! contact id, high-confidence matches only. Built by "
                 "src/build_act_lookup.py from data/interim/act_crosswalk.parquet. "
                 "A CRD absent here does not sync to Act!; it is logged locally "
                 "and nothing is written to a contact we are not sure of."),
        "built_utc": str(high.built_utc.max()) if "built_utc" in high else "",
        "contacts": m,
    }, separators=(",", ":"), sort_keys=True), encoding="utf-8")

    print(f"[*] {total:,} crosswalk rows -> {len(m):,} syncable CRDs")
    if mismatched:
        print(f"[!] dropped {len(mismatched)} whose Act! contact is a DIFFERENT "
              f"person with the same surname -- a call would have been logged "
              f"onto their namesake:")
        for crd, sec_name, act_name, namesakes in mismatched[:8]:
            print(f"      CRD {crd:<9} SEC {sec_name:<28} Act! {act_name}"
                  f"{f'  ({namesakes} namesakes)' if namesakes else ''}")
    if len(dupes):
        print(f"[!] dropped {len(dupes):,} rows whose CRD matched more than one "
              f"Act! contact -- ambiguous, so neither is used")
    for tier, n in df.tier.value_counts().items():
        print(f"      {tier:<8} {n:>7,}")
    print(f"[*] wrote {OUT}  ({OUT.stat().st_size:,} bytes)")
    print(f"[*] {total - len(m):,} advisors will log locally without reaching Act!")


if __name__ == "__main__":
    main()
