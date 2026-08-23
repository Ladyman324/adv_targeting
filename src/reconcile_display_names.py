"""Show the name an advisor actually goes by -> patches webapp/data in place.

    reads    data/output/advisors.parquet   the SEC feed's name fields
             webapp/data/contacts.json      what each advisor's FIRM publishes
    patches  webapp/data/pins_??.json       the name on the map pin
             webapp/data/advisor_index.json the name in national search

WHAT IS WRONG
-------------
export_national.py builds a display name as first_name, overridden by
used_first_name where the SEC feed has one. That is right for the large
majority -- 59,979 of the 73,548 records with a used_first_name are ordinary
nicknames, WILLIAM -> Bill, GREGORY -> Greg, JEFFREY -> Jeff.

For a minority the two fields are inverted at the source. Raj Sharma, CRD
1764403, is filed first_name=Raj, used_first_name=NAGANATH -- so the map called
him Naganath Sharma and the panel said "filed as Raj Sharma", which is backwards
twice over: IAPD shows Raj Sharma as the name he goes by and NAGANATH RAJ SHARMA
as the legal one.

WHY THIS IS NOT A GUESS
-----------------------
The advisor's own firm publishes what they go by, and this project already
collects it. Merrill lists him as "Raj Sharma", his address is raj_sharma@ml.com
and his profile page ends /raj_sharma. Three independent signals, none of them
ours, all disagreeing with used_first_name.

So nothing here is invented. The roster is used to CHOOSE BETWEEN NAMES THE SEC
ALREADY GAVE US -- if a roster says something absent from the SEC record, it is
ignored. That is the difference between fixing a name and importing one.

THE SAFEGUARDS, AND WHAT EACH ONE STOPS
---------------------------------------
Substituting the wrong person's name is far worse than showing a legal one, so
every condition below must hold:

  tier is confirmed or high    A `review` match is a name similarity, not an
                               identity: those records contradict the SEC on
                               state 42% of the time. 360 of the suspect
                               advisors have only a review match and are left
                               alone.

  surname agrees               Stops cross-person substitution outright. "Todd
                               Baker" can never overwrite "Jennifer Sharma".

  the first name is already    Nothing is imported. Raj is chosen because
  in the SEC record            first_name is already Raj.

  the first name is not        "B Scott Gioffre" would turn Bruno Gioffre into
  a bare initial               B Gioffre. An initial is not a better name than
                               a name, whatever the evidence says. 130 excluded.

  the email or profile URL     THE DECIDING TEST. Two independent signals rather
  carries the same name        than one string: raj_sharma@ml.com and a profile
                               page ending /raj_sharma both say Raj. 320 cases
                               fail this and are left alone -- among them
                               "Charlie Hatcher", whose Cetera record gives the
                               legal "JAMES DAVID HATCHER" and would have made
                               the display worse.

A rule excluding CRM-sourced records was tried and REMOVED. The reasoning was
that a CRM row holds the legal name while a roster publishes the marketing one,
which is true in general -- but it is a proxy for "no independent evidence", and
the corroboration test above measures that directly. The proxy also excluded the
case this script exists for: Raj Sharma's winning record is CRM-sourced, and its
email and profile URL are Merrill's own. Dropping the rule admitted 343 more,
every one of them corroborated, and did NOT admit Charlie Hatcher.

A SECOND, NARROWER ROUTE was added for names the SEC record does not contain at
all. Those are refused on principle above -- the point of this file is choosing
between names the SEC already gave us -- with one exception: where the firm's
name is a SHORTENING of the filed one and BOTH the address and the profile URL
carry it. See shortens_used(). That admits an everyday short form (Josesph ->
Joe, Katheryn -> Katie) and a misspelling in the filing (Timonthy -> Tim), and
refuses a different name (Wanda -> Thuyhuong, Carol -> Tommy).

Measured 2026-08-21: 2,975 advisors have a longer used_first_name; 1,334 satisfy
every condition -- 1,314 choosing among SEC names on one corroborating signal,
20 through the shortening rule on two. 0.3% of the file.

NOTHING IS HIDDEN. The name that stops being displayed becomes the "filed as"
line on the panel, so anyone checking against IAPD can see both and why.

Run:  python src/reconcile_display_names.py            patch in place
      python src/reconcile_display_names.py --dry-run  report, change nothing
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = ROOT / "webapp" / "data"
ADVISORS = ROOT / "data" / "output" / "advisors.parquet"

# A rule that starts renaming thousands of people has stopped being the narrow
# correction it was measured to be, and should stop rather than quietly reshape
# the map. 961 today; the ceiling leaves room for the roster set to grow.
CHANGE_CAP = 3000


def tokens(text: str) -> list:
    return [t for t in re.sub(r"[^A-Za-z ]", " ", str(text or "")).split() if t]


def first_token(text: str) -> str:
    t = tokens(text)
    return t[0].lower() if t else ""


def last_token(text: str) -> str:
    t = tokens(text)
    return t[-1].lower() if len(t) > 1 else ""


def corroborated(name: str, entry: dict, field: str = "") -> bool:
    """Does the firm's own email or profile URL carry this first name too?

    `field` narrows it to one signal -- "e" for the address, "pu" for the
    profile URL -- so a caller that needs BOTH can ask for them separately.
    """
    source = (f"{entry.get('e', '')} {entry.get('pu', '')}" if not field
              else str(entry.get(field, "")))
    parts = set(re.split(r"[^a-z]+", source.lower()))
    return first_token(name) in parts


# How much of the front of the name has to survive. Two letters keeps Joe for
# Josesph, Bo for Bogan, Katie for Katheryn; one letter would also admit
# Kirk for Gary, which is a different person's name, not a short form.
SHORTENING_PREFIX = 2


def shortens_used(used: str, candidate: str) -> bool:
    """Is the firm's name a SHORTENING of the filed one, rather than a new name?

    This is the only route by which a name absent from the SEC record reaches
    the map, so it is deliberately narrow: shorter, and starting the same way.

    It catches the two things a firm's own address is genuinely authoritative
    about -- an everyday short form (Josesph -> Joe, Katheryn -> Katie) and a
    misspelling in the filing (Timonthy -> Tim, Jamesd -> Jim).

    It does NOT catch Bill for William or Bob for Bernard, and that is on
    purpose. src/nicknames.py could answer those, and its own docstring says not
    to use it this way: "It is not a renaming table... The SEC feed holds the
    registration record and stays the display name." It is calibrated to
    OVER-accept, because a false "same person" there is corrected by other
    signals in the match score. Here it would be the only signal, and a false
    positive renames somebody. Three advisors are not worth borrowing an
    instrument tuned for a different question.

    It also refuses Wanda -> Thuyhuong, Carol -> Tommy and Anthony -> Kevin:
    different names, not different spellings, where the advisor's own statement
    to the regulator about what they go by should stand.
    """
    used, candidate = used.lower(), candidate.lower()
    return (len(candidate) < len(used)
            and candidate[:SHORTENING_PREFIX] == used[:SHORTENING_PREFIX])


def decide() -> dict:
    """crd -> (preferred display name, the name it replaces)."""
    adv = pd.read_parquet(ADVISORS, columns=[
        "advisor_crd", "first_name", "middle_name", "last_name", "used_first_name",
    ]).drop_duplicates("advisor_crd")
    adv["advisor_crd"] = adv["advisor_crd"].astype(str)
    contacts = json.loads((WEB / "contacts.json").read_text(encoding="utf-8"))["advisors"]

    out, skipped = {}, {"tier": 0, "surname": 0, "initial": 0, "not_in_sec": 0,
                        "no_corroboration": 0}
    for row in adv.itertuples(index=False):
        used = str(row.used_first_name or "").strip()
        first = str(row.first_name or "").strip()
        last = str(row.last_name or "").strip()
        if not used or not first or not last:
            continue
        # Only where used_first_name is currently WINNING and looks like a formal
        # name rather than a nickname. Bill, Greg and Jeff are shorter and are
        # never considered.
        if len(used) <= len(first):
            continue
        entry = contacts.get(row.advisor_crd)
        if not entry:
            continue
        if entry.get("t") not in ("confirmed", "high"):
            skipped["tier"] += 1
            continue
        published = str(entry.get("n") or "")
        if last_token(published) != last.lower():
            skipped["surname"] += 1
            continue
        candidate = first_token(published)
        # An INITIAL is not a better name than a name. Morgan Stanley publishes
        # "B Scott Gioffre", whose first token is "B" -- and first_name is "B"
        # too, so every other test passes while the change would turn "Bruno
        # Gioffre" into "B Gioffre". Replacing a name with a single letter is a
        # downgrade whatever the evidence says.
        if len(candidate) < 2:
            skipped["initial"] += 1
            continue
        known = {first.lower(), str(row.middle_name or "").strip().lower(), used.lower()}
        if candidate == used.lower():
            skipped["not_in_sec"] += 1
            continue
        if candidate in known:
            # Choosing between names the SEC already gave us. One signal is
            # enough, because nothing is being introduced.
            if not corroborated(published, entry):
                skipped["no_corroboration"] += 1
                continue
            out[row.advisor_crd] = (f"{first} {last}".title(), f"{used} {last}".title())
            continue
        # A name the SEC has never heard of -- see shortens_used() for the
        # narrow case where that is accepted, and for what it deliberately is
        # not.
        if not shortens_used(used, candidate):
            skipped["not_in_sec"] += 1
            continue
        if not (corroborated(published, entry, "e") and corroborated(published, entry, "pu")):
            skipped["no_corroboration"] += 1
            continue
        out[row.advisor_crd] = (f"{candidate.title()} {last}".title(),
                                f"{used} {last}".title())
    return out, skipped


def patch_pins(chosen: dict, dry: bool) -> int:
    """The name on the map pin is pins[i][7]."""
    changed = 0
    for path in sorted(WEB.glob("pins_??.json")):
        layer = json.loads(path.read_text(encoding="utf-8"))
        touched = 0
        for pin in layer["pins"]:
            pick = chosen.get(str(pin[6]))
            if pick and pin[7] != pick[0]:
                pin[7] = pick[0]
                touched += 1
        if touched and not dry:
            temp = path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(layer, separators=(",", ":")), encoding="utf-8")
            temp.replace(path)
        changed += touched
    return changed


def patch_index(chosen: dict, dry: bool) -> int:
    """Search name is row[1]; row[6] is what the panel calls "filed as"."""
    path = WEB / "advisor_index.json"
    index = json.loads(path.read_text(encoding="utf-8"))
    changed = 0
    for row in index["advisors"]:
        pick = chosen.get(str(row[0]))
        if not pick or row[1] == pick[0]:
            continue
        row[1] = pick[0]
        # The name we stopped showing goes here rather than nowhere. Without
        # this the panel would say "filed as Raj Sharma" underneath a card that
        # already says Raj Sharma.
        while len(row) < 7:
            row.append("")
        row[6] = pick[1]
        changed += 1
    if changed and not dry:
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(index, separators=(",", ":")), encoding="utf-8")
        temp.replace(path)
    return changed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    args = ap.parse_args()

    chosen, skipped = decide()
    print(f"[*] {len(chosen):,} advisors show the name their own firm publishes")
    print("    excluded: " + ", ".join(f"{k} {v:,}" for k, v in skipped.items()))
    if len(chosen) > CHANGE_CAP:
        raise SystemExit(
            f"{len(chosen):,} changes exceeds the {CHANGE_CAP:,} cap. This rule was "
            f"measured at 961; something upstream has changed and renaming a tenth "
            f"of the country is not a decision this script should make quietly.")

    for crd, (preferred, replaced) in list(chosen.items())[:8]:
        print(f"      CRD {crd:<9} {replaced:<26} -> {preferred}")

    pins = patch_pins(chosen, args.dry_run)
    index = patch_index(chosen, args.dry_run)
    print(f"[*] {pins:,} map pins and {index:,} search rows "
          f"{'would be' if args.dry_run else 'were'} rewritten")
    if args.dry_run:
        print("[*] dry run: nothing written")


if __name__ == "__main__":
    main()
