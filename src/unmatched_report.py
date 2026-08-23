"""Why 15,215 contact rows match no advisor -- grouped by CAUSE, not count.

The unmatched pile is where silent parser failures hide. Four bugs found this
session all looked identical from the outside: the row simply matched nobody.
Mariner lost 73% of its roster to trailing credentials, "Lynn Shaw, AWMATM"
gated on "awmatm", Collie Krausnick's nickname appears in no SEC field. None
raised an error; each just added to this number.

So the number itself is useless. What matters is WHY, and the causes are
separable:

    no_surname      the name did not parse to anything usable
    surname_unknown the surname exists nowhere in the SEC index -- a parse
                    artefact, a non-person, or a genuinely absent advisor
    no_firm_match   candidates share the surname but none at this firm
    scored_low      real candidates, but nothing agreed well enough

Only the first two are usually OUR bug. The last two are frequently correct
refusals -- support staff who are not registered, advisors who left.

Run:  python src/unmatched_report.py [--sample N]
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import pandas as pd

import build_contacts as B
from forbes_match import build_index, load_reference, name_score, split_name


def classify(people: pd.DataFrame, index) -> pd.DataFrame:
    rows = []
    for rec in people.itertuples(index=False):
        given, last = split_name(rec.name)
        cands = index.get(last, [])
        if not last:
            why = "no_surname"
        elif not cands:
            why = "surname_unknown"
        else:
            best_n = max((name_score(given, f) for _, f, _ in cands), default=0.0)
            at_firm = any(rec.firm_crd and rec.firm_crd in e["firms"] for _, _, e in cands)
            if best_n <= 0 and not at_firm:
                # Distinct from surname_unknown: the surname IS in the index,
                # it is the given name that agrees with nobody. Conflating the
                # two hid which of these are our parse bugs and which are
                # people the SEC simply does not list.
                why = "no_given_match"
            elif not at_firm:
                why = "no_firm_match"
            else:
                why = "scored_low"
        rows.append({"name": rec.name, "email": rec.email, "source": rec.source,
                     "firm_crd": rec.firm_crd, "surname": last, "why": why})
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sample", type=int, default=12)
    args = ap.parse_args()

    domains = B.derive_domain_map()
    people = pd.concat([f for f in (B.load_crm(domains), B.load_rosters()) if len(f)],
                       ignore_index=True)
    keep = ["name", "title", "email", "phone", "mobile", "city", "state", "company",
            "owner", "assets", "team_key", "firm_crd", "phone_kind", "source", "source_file"]
    people = people[keep].fillna({"team_key": "", "owner": "", "phone_kind": ""})
    people["phone_kind"] = B.infer_phone_kind(people)
    people = people[people["name"].astype(str).str.strip() != ""]

    adv, br, emp, _, _ = load_reference()
    index = build_index(adv, br, emp)
    scored = B.score_contacts(people, index)
    lost = scored[scored["tier"] == "none"].copy()
    print(f"[*] {len(lost):,} unmatched of {len(scored):,} rows\n")

    diag = classify(lost, index)
    print("BY CAUSE")
    for why, n in diag["why"].value_counts().items():
        print(f"  {why:18} {n:>7,}  ({n / len(diag):.0%})")

    print("\nBY SOURCE (worst 10 by share of that source's rows lost)")
    tot = scored.groupby("source").size()
    bad = diag.groupby("source").size()
    share = (bad / tot).dropna().sort_values(ascending=False)
    for src, pct in share.head(10).items():
        print(f"  {src[:32]:34} {int(bad[src]):>6,} of {int(tot[src]):>6,}  {pct:.0%}")

    print("\nNAME SHAPE among unmatched")
    shapes = collections.Counter()
    for n in diag["name"]:
        s = str(n)
        if "," in s: shapes["contains a comma"] += 1
        if re.search(r"\d", s): shapes["contains a digit"] += 1
        if len(s.split()) == 1: shapes["single token"] += 1
        if len(s.split()) > 4: shapes["5+ tokens"] += 1
        if s.isupper(): shapes["ALL CAPS"] += 1
        if re.search(r"[^\x00-\x7f]", s): shapes["non-ascii"] += 1
        if re.search(r"\b(?:team|group|wealth|advisors|llc|inc)\b", s, re.I):
            shapes["looks like a TEAM not a person"] += 1
    for k, v in shapes.most_common():
        print(f"  {k:32} {v:>7,}")

    print("\nMOST COMMON UNMATCHED SURNAMES (a parse artefact repeats)")
    for sur, n in collections.Counter(diag.loc[diag.surname != "", "surname"]).most_common(12):
        print(f"  {sur:22} {n:>5}")

    for why in ("no_surname", "surname_unknown", "no_given_match", "no_firm_match", "scored_low"):
        sub = diag[diag.why == why]
        if not len(sub):
            continue
        print(f"\nSAMPLE -- {why} ({len(sub):,})")
        for r in sub.sample(min(args.sample, len(sub)), random_state=1).itertuples():
            print(f"  {str(r.name)[:34]:36} {str(r.email)[:38]:40} {r.source[:22]}")


if __name__ == "__main__":
    main()
