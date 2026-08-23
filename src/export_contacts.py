"""Contact details -> webapp/data/contacts.json, keyed on advisor CRD.

TRIAL SCOPE: EIC's own 16 staff, from data/raw/EIC_Contacts.xlsx. Testing
click-to-call and click-to-email on people who can tell us it went wrong is a
great deal cheaper than testing it on prospects.

It is also a real test of the matcher, because the contact list uses preferred
names while the SEC filings use legal ones -- Bo for Robert, Tate for Edison,
Terry for Richard Terrence, Paul for John Paul. All sixteen resolve.

Phone numbers are normalised to E.164 (+15551234567) because that is what a
tel: link needs. The display form is kept separately so the card can show the
number the way a person reads it.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from collections import defaultdict

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from forbes_match import split_name, given_forms, name_score, norm  # noqa: E402

ROOT = pathlib.Path(__file__).parents[1]
OUT = ROOT / "webapp" / "data"
EIC_CRD = "283930"


def e164(raw: str) -> str:
    """US 10-digit -> +1XXXXXXXXXX. Anything else is returned empty rather than
    guessed: a malformed tel: link dials something, and it will not be the
    person on the card."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"+1{digits}" if len(digits) == 10 else ""


def pretty(raw: str) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else str(raw or "")


def roster(firm_crd: str):
    advisors = pd.read_parquet(
        ROOT / "data" / "interim" / "advisors.parquet",
        columns=["advisor_crd", "first_name", "middle_name", "last_name",
                 "suffix", "used_first_name"])
    advisors["advisor_crd"] = advisors["advisor_crd"].astype(str)
    branches = pd.read_parquet(ROOT / "data" / "output" / "advisor_branches.parquet",
                               columns=["advisor_crd", "firm_crd"])
    branches["advisor_crd"] = branches["advisor_crd"].astype(str)
    branches["firm_crd"] = branches["firm_crd"].astype(str)
    ids = set(branches[branches["firm_crd"] == firm_crd]["advisor_crd"])
    sub = advisors[advisors["advisor_crd"].isin(ids)].copy()
    sub["last_key"] = sub["last_name"].map(
        lambda v: norm(re.sub(r"\([^)]*\)", " ", str(v or ""))))
    index = defaultdict(list)
    for row in sub.itertuples():
        index[row.last_key].append((row.advisor_crd, given_forms(row)))
    return index


def main() -> None:
    source = ROOT / "data" / "raw" / "EIC_Contacts.xlsx"
    if not source.exists():
        raise SystemExit(f"{source} not found")
    people = pd.read_excel(source, dtype=str).fillna("")
    index = roster(EIC_CRD)

    contacts, unmatched, ambiguous = {}, [], []
    for row in people.itertuples(index=False):
        name, work, cell, email = row[0], row[1], row[2], row[3]
        given, last = split_name(name)
        hits = [h for h in index.get(last, []) if name_score(given, h[1]) >= 0.8]
        if len(hits) != 1:
            (ambiguous if hits else unmatched).append(name)
            continue
        crd = hits[0][0]
        contacts[crd] = {
            "n": name.strip(),
            "e": email.strip(),
            "w": e164(work), "wd": pretty(work),
            "c": e164(cell), "cd": pretty(cell),
            "src": "EIC directory",
            # every one of these was matched on name within a known firm, so
            # the tier is honest about how the CRD was established
            "t": "matched",
        }

    payload = {"advisors": contacts,
               "note": "Internal EIC directory. Trial data for the contact panel."}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "contacts.json").write_text(json.dumps(payload, separators=(",", ":")),
                                       encoding="utf-8")

    print(f"contacts.json  {len(contacts)} of {len(people)} matched to a CRD")
    bad_phone = sum(1 for v in contacts.values() if not v["w"] or not v["c"])
    print(f"  phones normalised to E.164; {bad_phone} row(s) with an unparseable number")
    if unmatched:
        print(f"  NO MATCH: {', '.join(unmatched)}")
    if ambiguous:
        print(f"  AMBIGUOUS: {', '.join(ambiguous)}")


if __name__ == "__main__":
    main()
