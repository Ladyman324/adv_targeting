"""territories.yaml -> webapp/data/territories.json, for the app to resolve state -> owner.

WHY THIS EXISTS
---------------
The CRM's "EIC Contact" field cannot be read through the Act! Web API. The
system column REFERREDBY carries displayName "EIC Contact", but the JSON
property `referredBy` resolves to a DIFFERENT custom field, and the system one
is shadowed. Tested: `$select=REFERREDBY` returns null.

It does not need to be read. Territory assignment is a function of state, and a
more accurate one than the field it replaces -- the field is blank on 9,223 LPL
contacts nobody ever tagged, while each salesperson is responsible for the LPL
advisors in their territory whether or not the record says so. Validated out of
sample: the map was built from non-LPL records, then tested against the 6,141
LPL contacts that HAD been assigned by hand, and matched 6,140 of them.

WHY A LOOKUP FILE RATHER THAN A COLUMN ON EVERY ADVISOR
-------------------------------------------------------
51 states against 127,445 advisors. Shipping the mapping instead of the answer
keeps it under a kilobyte, works in both the desktop and field views without
rebuilding tiles or contacts.json, and means a territory change is a one-line
edit to territories.yaml rather than a full pipeline run.

WHAT IT IS FOR
--------------
A TERRITORY NOTICE -- "this advisor is in TN and is assigned to Matt Keeter" --
telling a rep they are looking outside their own patch. It is NOT a claim that a
relationship exists; that is a separate fact carried by has_clients___eic and
the book of business, and it gets its own display.

Run:  python src/build_territories.py
"""
from __future__ import annotations

import json
import pathlib

import yaml

ROOT = pathlib.Path(__file__).parents[1]
SRC = ROOT / "territories.yaml"
OUT = ROOT / "webapp" / "data" / "territories.json"

# The 50 states, DC, and the two Caribbean territories that belong to a
# salesperson. GU is deliberately absent: nobody covers it, and the app says
# nothing rather than guessing.
US = {"AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY",
      "LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND",
      "OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC",
      "PR","VI"}


def main() -> None:
    t = yaml.safe_load(SRC.read_text(encoding="utf-8"))
    people, terr = t["people"], t["territories"]

    by_state, problems = {}, []
    for code, states in terr.items():
        person = people.get(code)
        if not person:
            problems.append(f"territory {code} has no entry under people")
            continue
        if person.get("inactive"):
            problems.append(f"territory {code} ({person.get('name')}) is an INACTIVE Act! user")
        for s in states:
            if s in by_state:
                problems.append(f"{s} is in two territories: {by_state[s]['c']} and {code}")
            by_state[s] = {"c": code, "n": person["name"], "e": person["email"]}

    missing = sorted(US - set(by_state))
    if missing:
        problems.append(f"no territory covers {missing}")
    extra = sorted(set(by_state) - US)
    if extra:
        problems.append(f"unknown state codes {extra}")
    if problems:
        raise SystemExit("[!] territories.yaml is not usable:\n   " + "\n   ".join(problems))

    payload = {
        "note": ("State -> the salesperson responsible for advisors there. A TERRITORY "
                 "assignment, not a claim that a relationship exists. Maintained by hand "
                 "in territories.yaml; see that file for provenance."),
        "as_of": str(t.get("as_of", "")),
        # National/overlay roles are NOT territorial -- JPP appears in 36 states
        # and RTI in 26, always as a small minority. Carried so the app can name
        # them where the CRM does, without the state rule ever producing one.
        "national": {c: {"n": people[c]["name"], "e": people[c]["email"]}
                     for c in t.get("national", []) if c in people},
        "states": by_state,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True), encoding="utf-8")

    n = len({v["c"] for v in by_state.values()})
    print(f"[*] {len(by_state)} states across {n} territories, as of {payload['as_of']}")
    for c in sorted(terr):
        print(f"      {c:<4} {people[c]['name']:<22} {len(terr[c]):>2} states")
    print(f"[*] wrote {OUT}  ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
