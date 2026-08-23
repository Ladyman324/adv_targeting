"""Measure the contact matcher against a labelled set, instead of trusting it.

THE PROBLEM THIS SOLVES
-----------------------
score_contacts() attaches 126,352 contact rows to SEC advisor CRDs, and until
now nothing has ever checked whether it attaches them to the RIGHT ones. The
thresholds (ACCEPT 0.72, MARGIN 0.12, namesake cap 3) were inherited from
forbes_match, which was calibrated on a DIFFERENT input: Barron's rows carry a
firm name matched fuzzily, contact rows carry a firm CRD resolved exactly from
an email domain, and the weights differ accordingly (W_FIRM 0.30 vs 0.30 but
W_NAME 0.45 vs 0.42, W_CITY 0.10 vs 0.13). Borrowed calibration is not
calibration. 32,574 records ship in the "review" tier with a warning and 1,582
score below 0.30 purely on a guess about where the cut belongs.

THE BRIDGE
----------
Barron's publishes advisor_crd alongside the advisor's name, firm and city --
1,788 rows, 1,522 distinct CRDs, and it is the only place in this project where
a person's identity and their CRD appear together from a source that is not the
SEC. That makes it usable as truth.

But Barron's rows are not contact rows, and scoring Barron's rows directly
would measure a matcher on input it never sees. So the bridge runs the other
way: take the REAL contact rows -- the same frame build_contacts ships, with
their real emails, their real domain-derived firm CRDs, their real city fields
and all their real mess -- and label the subset of them that are Barron's
people. Their true CRD then comes from Barron's, and the matcher's answer can
be graded.

WHY THIS IS NOT CIRCULAR
------------------------
The join is contact row <-> Barron's row. Both are non-SEC sources. The thing
being graded is contact row <-> SEC advisor. The CRD never appears on the
contact side, and never participates in the join. What the join asks is only
"is this contact row the same human as this Barron's row", which the matcher
is not consulted about.

THE BIAS THAT REMAINS, STATED PLAINLY
-------------------------------------
The join requires the contact row's name to agree with Barron's, and (unless
the surname is unique nationally) its city or firm to agree too. A contact row
with a mangled name or a blank city is therefore harder to LABEL -- and is also
harder to MATCH. So the labelled subset is cleaner than the population, and
every precision number here is an optimistic bound, not an estimate. It is
still the only measurement available, and a matcher that fails on clean rows
certainly fails on dirty ones. Coverage is printed so the gap is visible.

Barron's advisors also skew large-firm and large-book, which is the population
the sales team cares most about -- so the bias runs toward the rows that matter.

    python src/contact_calibrate.py             # grade at the shipping settings
    python src/contact_calibrate.py --sweep     # precision/recall by threshold
    python src/contact_calibrate.py --errors 40 # show what it gets wrong
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import pandas as pd

from build_contacts import (CONTACT_NAMESAKE_CAP, load_index, load_people,
                            score_contacts, strip_designations)
from forbes_match import ACCEPT, MARGIN, norm, split_name

ROOT = pathlib.Path(__file__).resolve().parents[1]
BARRONS = ROOT / "data" / "interim" / "barrons_rankings.parquet"


def given_tokens(name: str) -> list[str]:
    given, _ = split_name(strip_designations(str(name or "")))
    return [t for t in given if t]


def given_agree(a: list[str], b: list[str]) -> bool:
    """Do two published given names refer to the same person?

    Deliberately generous on FORM (Rob/Robert, J./James) and strict on
    SUBSTANCE (Robert != Richard). The join only has to decide identity between
    two human-written renderings of the same name; it is not the matcher.
    """
    if not a or not b:
        return False
    x, y = a[0], b[0]
    if x == y:
        return True
    if len(x) == 1 or len(y) == 1:          # initial vs full: J. == James
        return x[0] == y[0]
    # one a prefix of the other, at least three characters: rob/robert, chris/
    # christopher. Three, not two, because "al" prefixes both Alan and Albert.
    short, long = sorted((x, y), key=len)
    return len(short) >= 3 and long.startswith(short)


def firm_agree(contact_company: str, barrons_firm: str) -> bool:
    a, b = norm(contact_company), norm(barrons_firm)
    if not a or not b:
        return False
    at, bt = set(a.split()), set(b.split())
    # ignore the words every firm name contains
    noise = {"llc", "inc", "lp", "group", "financial", "advisors", "advisory",
             "wealth", "management", "partners", "capital", "the", "and", "of"}
    at, bt = at - noise, bt - noise
    return bool(at & bt)


def load_truth() -> pd.DataFrame:
    b = pd.read_parquet(BARRONS)
    b["advisor_crd"] = b["advisor_crd"].astype(str).str.strip()
    b = b[b["advisor_crd"].str.fullmatch(r"\d+")].copy()
    b["last_key"] = [split_name(strip_designations(n))[1] for n in b["advisor_name"]]
    b["given"] = [given_tokens(n) for n in b["advisor_name"]]
    b["city_key"] = b["location"].map(norm)
    b["state_key"] = b["rank_state"].astype(str).str.strip().str.upper()
    b = b[b["last_key"] != ""]
    # One person can appear in several rankings (top1500 and women, say). Keep
    # one row per CRD, and DROP any CRD that Barron's itself renders under two
    # different surnames -- if the source disagrees with itself, it is not truth.
    keep = []
    for crd, group in b.groupby("advisor_crd"):
        if group["last_key"].nunique() == 1:
            keep.append(group.iloc[0])
    return pd.DataFrame(keep).reset_index(drop=True)


def bridge(people: pd.DataFrame, truth: pd.DataFrame) -> pd.DataFrame:
    """Label contact rows with the CRD Barron's published for that person.

    A label is only issued where the pairing is UNAMBIGUOUS IN BOTH
    DIRECTIONS: exactly one Barron's person can be this contact, and exactly
    one contact row can be that Barron's person. Anything a human would have to
    think about is dropped rather than guessed -- a wrong label does not just
    lose a row, it teaches the threshold the wrong lesson.
    """
    by_surname: dict[str, list] = {}
    for row in truth.itertuples(index=False):
        by_surname.setdefault(row.last_key, []).append(row)

    people = people.copy()
    people["bridge_last"] = [split_name(strip_designations(n))[1] for n in people["name"]]
    people["bridge_given"] = [given_tokens(n) for n in people["name"]]

    pairs = []
    for i, rec in zip(people.index, people.itertuples(index=False)):
        cands = by_surname.get(rec.bridge_last, [])
        if not cands:
            continue
        hits = []
        for cand in cands:
            if not given_agree(rec.bridge_given, cand.given):
                continue
            city_ok = bool(rec.city) and norm(rec.city) == cand.city_key
            state_ok = bool(rec.state) and rec.state == cand.state_key
            firm_ok = firm_agree(getattr(rec, "company", ""), cand.firm_name_barrons)
            # A name alone is enough ONLY when that full name is unique on both
            # sides nationally. Otherwise geography or firm has to corroborate.
            if city_ok or firm_ok or (state_ok and len(cands) == 1):
                hits.append(cand)
        if len(hits) == 1:
            pairs.append((i, hits[0].advisor_crd))

    labelled = pd.DataFrame(pairs, columns=["row", "truth_crd"])
    # Reverse uniqueness: if six contact rows all claim to be one Barron's
    # advisor, at most one of them is, and we cannot say which.
    solo = labelled["truth_crd"].value_counts()
    labelled = labelled[labelled["truth_crd"].isin(solo[solo == 1].index)]
    out = people.loc[labelled["row"]].copy()
    out["truth_crd"] = labelled.set_index("row")["truth_crd"]
    return out.drop(columns=["bridge_last", "bridge_given"])


def grade(scored: pd.DataFrame, accept: float, margin: float,
          cap: int) -> tuple[int, int, int]:
    """(accepted, correct, recoverable) at one set of thresholds.

    Every one of the three gates is re-derived from a stored column
    (match_score, match_gap, namesakes), so a sweep needs no re-matching and
    each gate can be moved independently -- which matters, because they turn
    out to bind very unequally.
    """
    picked = ((scored["match_score"] >= accept)
              & (scored["match_gap"] >= margin)
              & (scored["namesakes"] <= cap))
    accepted = int(picked.sum())
    correct = int((picked & (scored["advisor_crd"] == scored["truth_crd"])).sum())
    recoverable = int((scored["advisor_crd"] == scored["truth_crd"]).sum())
    return accepted, correct, recoverable


def sweep(scored: pd.DataFrame, label: str, values, build) -> None:
    """One gate at a time, the other two held at their shipping values."""
    print(f"\n  {label:>10}  accepted  correct  precision   recall")
    for v in values:
        a, c, _ = grade(scored, *build(v))
        if a:
            print(f"  {v:>10}  {a:8,}  {c:7,}     {c / a:6.1%}  "
                  f"{c / len(scored):6.1%}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sweep", action="store_true", help="precision/recall by threshold")
    ap.add_argument("--errors", type=int, default=15, help="how many mistakes to print")
    ap.add_argument("--limit", type=int, help="only the first N contact rows")
    args = ap.parse_args()

    people = load_people(args.limit)
    truth = load_truth()
    print(f"[*] Barron's truth: {len(truth):,} advisors with a published CRD")

    labelled = bridge(people, truth)
    print(f"[*] bridge: {len(labelled):,} contact rows labelled "
          f"({len(labelled) / len(truth):.0%} of the truth set reached; "
          f"{len(labelled) / len(people):.2%} of all contact rows)")
    if len(labelled) < 100:
        print("[!] too few labelled rows to calibrate on -- reporting anyway, "
              "but do not move a threshold on this")

    index = load_index()
    scored = score_contacts(labelled, index)

    accepted, correct, recoverable = grade(scored, ACCEPT, MARGIN, CONTACT_NAMESAKE_CAP)
    print(f"\nAt the shipping settings (ACCEPT {ACCEPT}, MARGIN {MARGIN}, "
          f"CONTACT_NAMESAKE_CAP {CONTACT_NAMESAKE_CAP}):")
    print(f"  accepted   {accepted:,} of {len(scored):,}")
    print(f"  precision  {correct / accepted:.1%}" if accepted else "  precision  n/a")
    print(f"  recall     {correct / len(scored):.1%}")
    print(f"  ceiling    {recoverable / len(scored):.1%} "
          f"(the matcher's top pick is right this often, before any threshold)")

    # What the review tier is actually worth -- the open question this whole
    # exercise exists to answer.
    review = scored[scored["tier"] == "review"]
    if len(review):
        right = (review["advisor_crd"] == review["truth_crd"]).mean()
        print(f"\nReview tier: {len(review):,} rows, {right:.1%} of them correct")
        for lo, hi in ((0.0, 0.30), (0.30, 0.50), (0.50, 0.60),
                       (0.60, 0.72), (0.72, 1.01)):
            band = review[(review["match_score"] >= lo) & (review["match_score"] < hi)]
            if len(band):
                acc = (band["advisor_crd"] == band["truth_crd"]).mean()
                print(f"  score {lo:.2f}-{hi:.2f}  {len(band):5,} rows  {acc:6.1%} correct")

    if args.sweep:
        # Each gate moved alone, the other two held at shipping values. Sweeping
        # them together hides which one is doing the work.
        sweep(scored, "ACCEPT", (0.40, 0.50, 0.60, 0.66, 0.72, 0.78, 0.84, 0.90),
              lambda v: (v, MARGIN, CONTACT_NAMESAKE_CAP))
        sweep(scored, "MARGIN", (0.00, 0.02, 0.04, 0.06, 0.08, 0.12, 0.16, 0.20),
              lambda v: (ACCEPT, v, CONTACT_NAMESAKE_CAP))
        sweep(scored, "NAMESAKES", (1, 2, 3, 4, 6, 10, 99),
              lambda v: (ACCEPT, MARGIN, v))

    wrong = scored[(scored["tier"] == "high")
                   & (scored["advisor_crd"] != scored["truth_crd"])]
    print(f"\nFALSE POSITIVES in the shipped 'high' tier: {len(wrong):,}")
    for row in wrong.head(args.errors).itertuples(index=False):
        print(f"  {str(row.name)[:26]:<26} {row.city[:14]:<14} "
              f"picked {row.advisor_crd:<9} truth {row.truth_crd:<9} "
              f"score {row.match_score}  src {row.source}")


if __name__ == "__main__":
    main()
