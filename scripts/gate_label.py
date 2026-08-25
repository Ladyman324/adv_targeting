"""Build data/interim/gate_truth.csv: a labelled sample the gate can be scored on.

THE RULE THIS FILE OBEYS
------------------------
A label may never be read off the Act! name, because the Act! name is what the
gate is being judged on. Labelling "Dave Harris -> Shanicia Harris" DIFFERENT
because the names look different would produce a truth set that agrees with any
name-comparison rule by construction, and would measure nothing at all.

So every label here comes from a witness that is not the Act! name field:

  EMAIL   the local part of the firm-issued address on the Act! record. The firm
          assigns it, and it states who owns the mailbox: `wayne.campbell@lpl`
          is Wayne Campbell's mailbox whatever the CRM's name field says.
  PHONE   the Act! businessPhone, looked up in the scraped firm rosters in
          data/raw/firm_rosters. A direct line that appears against exactly one
          person on the firm's own website names that person. Branch and office
          numbers are excluded outright -- a switchboard names nobody.
  ADDRESS the Act! businessAddress against the CRD's branches in
          advisor_branches.parquet. RECORDED, NEVER DECISIVE: an address says
          which office, and two different people sit in one office. It appears
          in the evidence string as corroboration and it never sets a label.

Anything these cannot settle is UNKNOWN, and the UNKNOWN rate is reported rather
than hidden. A guessed label is worse than a missing one: it moves the measured
numbers without moving the truth.

THE SAMPLE
----------
Stratified over (today's gate agrees / disagrees) x (high / review) x (has email
/ no email), because those eight cells behave very differently and a flat random
sample would be ~90% "agrees, high, has email" and would say nothing about the
cells the argument is actually in.

Two draws, kept apart in a `draw` column so nothing is quietly mixed:
  random  a fixed-seed random sample of the stratum -- the only rows from which
          an unbiased rate can be estimated.
  phone   extra rows drawn from the no-email cells that a roster phone can
          settle. Deliberately enriched, so they are excluded from the unbiased
          estimate and reported separately. Without them the no-email cells
          would be ~95% UNKNOWN and invisible.
  anchor  the eight hand-verified cases, always included, never counted in a
          rate.

Run:  python scripts/gate_label.py
"""
from __future__ import annotations

import argparse
import collections
import glob
import pathlib
import re
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from nicknames import same_person                                    # noqa: E402
from gate_eval import (ANCHORS, GENERIC_DOMAINS, _norm, _norm_street,  # noqa: E402
                       branch_index, email_parts, filed_split,
                       initials_agree, load, rule_a)

OUT = ROOT / "data" / "interim" / "gate_truth.csv"
ROSTERS = ROOT / "data" / "raw" / "firm_rosters"

SEED = 20260824
PER_EMAIL_CELL = 60
PER_NOEMAIL_CELL = 45
PHONE_ENRICH_PER_CELL = 30


# --------------------------------------------------------------------------
# the phone witness
# --------------------------------------------------------------------------

def roster_phone_owners() -> dict:
    """phone digits -> the one person the firm's own roster puts on that line.

    Two guards, both necessary:

      * columns named branch/office/main/site are SWITCHBOARDS. Any number
        appearing in one is discarded from the map entirely, even if a person
        column also carries it -- a whole branch shares it and it identifies
        nobody.
      * a number surviving that must map to exactly ONE distinct person name
        across every roster. Two names on one line is a team, not a witness.
    """
    person: dict = collections.defaultdict(set)
    switchboard: set = set()
    for path in sorted(glob.glob(str(ROSTERS / "*.csv"))):
        frame = pd.read_csv(path, low_memory=False)
        lower = {c.lower(): c for c in frame.columns}
        name_col = None
        for cand in ("name", "faname", "display name", "marketingname",
                     "full_name", "advisor_name"):
            if cand in lower:
                name_col = lower[cand]
                break
        if name_col is None:
            first = next((lower[c] for c in ("first_name", "first name") if c in lower), None)
            last = next((lower[c] for c in ("last_name", "last name") if c in lower), None)
            if first and last:
                frame["_name"] = (frame[first].fillna("").astype(str) + " "
                                  + frame[last].fillna("").astype(str))
                name_col = "_name"
        if name_col is None:
            continue
        for col in frame.columns:
            if not re.search(r"phone|tel", col, re.I):
                continue
            if re.search(r"kind|label|ext|count|status|type", col, re.I):
                continue
            shared = bool(re.search(r"branch|office|main|site|firm", col, re.I))
            for nm, raw in zip(frame[name_col].fillna(""), frame[col].fillna("")):
                digits = re.sub(r"\D", "", str(raw))
                if len(digits) == 11 and digits.startswith("1"):
                    digits = digits[1:]
                if len(digits) != 10:
                    continue
                if shared:
                    switchboard.add(digits)
                    continue
                nm = re.sub(r"\s+", " ", str(nm)).strip()
                if nm:
                    person[digits].add((nm, pathlib.Path(path).name))
    owners = {}
    for digits, names in person.items():
        if digits in switchboard:
            continue
        distinct = {_norm(n) for n, _ in names}
        if len(distinct) != 1:
            continue
        nm, src = sorted(names)[0]
        toks = [t for t in re.split(r"[\s,\.]+", nm) if t]
        # A personal name, not "Smith Wealth Advisors": two or three alphabetic
        # tokens. Anything else is a practice and cannot name a human.
        if not (2 <= len(toks) <= 3) or not all(re.match(r"^[A-Za-z'\-]+$", t) for t in toks):
            continue
        owners[digits] = (nm, src)
    return owners


# --------------------------------------------------------------------------
# labelling
# --------------------------------------------------------------------------

def sec_string(row) -> str:
    parts = [str(row.first_name or ""), str(row.middle_name or ""),
             str(row.last_name or "")]
    text = " ".join(p for p in parts if p and p != "None")
    used = str(row.used_first_name or "").strip()
    return text + (f" [files also as {used}]" if used else "")


def _given_agrees(given: str, row) -> bool:
    """Permissive agreement -- the project's own nickname table, as the gate uses it."""
    toks = filed_split(row)
    if not toks:
        return False
    return (any(same_person(given, t) for t in toks)
            or initials_agree(given + " " + str(row.last_name), toks))


def _agrees_strictly(given: str, row) -> bool:
    """Agreement that does NOT depend on the nickname table being right.

    A label built on nicknames.py would be measuring nicknames.py. So SAME is
    only ever asserted on evidence that stands without it: the same word, a
    truncation of it, the initials of it, or the used name the SEC itself files.
    "Bob" against a filed ROBERT is left UNKNOWN here even though it is almost
    certainly the same man -- the point of a truth set is that it does not have
    to be argued with.
    """
    g = _norm(given)
    if not g:
        return False
    toks = [t for t in (_norm(t) for t in filed_split(row)) if t]
    if not toks:
        return False
    if g in toks:
        return True
    for t in toks:
        if len(g) >= 3 and len(t) >= 3 and (t.startswith(g) or g.startswith(t)):
            return True
    return initials_agree(given + " " + str(row.last_name), toks)


def _differs_strictly(given: str, row) -> bool:
    """A disagreement solid enough to assert DIFFERENT.

    Three conditions, all required:
      * a real given name, not an initial;
      * no agreement even under the permissive nickname table -- so a label can
        never contradict the very table the rules rely on;
      * a different first letter. Jay/Jeffrey, Trigg/Thomas and every other
        diminutive nobody has written down yet begin with the letter they stand
        for, and those rows are left UNKNOWN rather than counted as mismatches.
    """
    g = _norm(given)
    toks = [t for t in (_norm(t) for t in filed_split(row)) if t]
    if len(g) < 3 or not toks:
        return False
    if _given_agrees(given, row):
        return False
    return all(t[0] != g[0] for t in toks)


def address_note(row, branches: dict) -> str:
    if not row.act_line1:
        return "no Act! business address"
    want = (_norm_street(row.act_line1), row.act_postal, _norm(row.act_city))
    filed = branches.get(row.advisor_crd, set())
    if not filed:
        return "SEC files no branch for this CRD"
    for street, postal, city in filed:
        if street and street == want[0]:
            return f"Act! address '{row.act_line1}' matches an SEC branch street for CRD {row.advisor_crd}"
    for street, postal, city in filed:
        if postal and postal == want[1] and city and city == want[2]:
            return f"Act! address is in the same city/ZIP as an SEC branch of CRD {row.advisor_crd}"
    return f"Act! address '{row.act_line1}, {row.act_city}' matches no SEC branch of CRD {row.advisor_crd}"


def label_row(row, owners: dict, branches: dict):
    """-> (label, channel, evidence). Names on the Act! record are never read."""
    addr = address_note(row, branches)

    given, has_surname, domain, how = email_parts(row)
    if given and has_surname and domain not in GENERIC_DOMAINS:
        head = (f"Act! email {row.email} ({how}) gives given name '{given}'; "
                f"SEC filing {sec_string(row)}")
        if _agrees_strictly(given, row):
            return "SAME", "email", (
                f"{head} -- the firm's own mailbox carries the filed given name. "
                f"Corroboration: {addr}")
        if _differs_strictly(given, row):
            return "DIFFERENT", "email", (
                f"{head} -- the firm's own mailbox names a different person of "
                f"this surname, sharing no given name and not even an initial "
                f"with the filing. Corroboration: {addr}")
        # Everything between the two: a bare initial (`jsmith` fits John, Joan
        # and a middle initial), or a name that agrees only through the nickname
        # table, or one that disagrees but shares its first letter.
        email_note = f"{head} -- not decisive either way"
    elif given and has_surname:
        email_note = (f"Act! email {row.email} is a personal domain -- the local "
                      f"part is self-chosen and is not the firm's statement")
    else:
        email_note = f"Act! email cannot be read as this person's name ({how})"

    owner = owners.get(row.act_phone or "")
    if owner:
        nm, src = owner
        toks = [_norm(t) for t in re.split(r"[\s,\.]+", nm) if _norm(t)]
        surname = toks[-1]
        givens = toks[:-1]
        head = (f"Act! businessPhone {row.act_phone} is the direct line of "
                f"'{nm}' in {src}; SEC filing {sec_string(row)}")
        if surname == _norm(row.last_name) and any(_agrees_strictly(g, row) for g in givens):
            return "SAME", "phone", (f"{head} -- the firm puts this advisor on "
                                     f"this line. Corroboration: {addr}")
        # A ROSTER NAME THAT DISAGREES IS NOT EVIDENCE OF ANYTHING, and this
        # branch used to say it was. It labelled 48 rows DIFFERENT, including
        # Act! "Ellen Takagi-Walsh" against SEC ELLEN MITSUE TAKAGI-WALSH --
        # plainly one person -- because 617 725 2000 is the number the RBC
        # roster happens to print against Joel Slovin. Act! stores whatever
        # number the desk had, which is very often the team or floor line, and
        # a shared line names a colleague, not an impostor. Only agreement
        # survives here; disagreement means the phone cannot settle the row.
        phone_note = (f"{head} -- the roster attributes this line to somebody "
                      f"else, which a shared team line does too, so it settles "
                      f"nothing")
    elif row.act_phone:
        phone_note = (f"Act! businessPhone {row.act_phone} is in no firm roster, "
                      f"or is a shared branch line")
    else:
        phone_note = "no Act! businessPhone"

    return "UNKNOWN", "", f"{email_note}. {phone_note}. Address: {addr}"


# --------------------------------------------------------------------------
# the sample
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()

    frame = load()
    frame["gate_a"] = [rule_a(r) for r in frame.itertuples(index=False)]
    frame["has_email"] = frame["email"] != ""
    frame["stratum"] = [
        f"{'agrees' if g else 'disagrees'}|{t}|{'email' if e else 'no-email'}"
        for g, t, e in zip(frame.gate_a, frame.tier_pre, frame.has_email)]

    owners = roster_phone_owners()
    print(f"[*] {len(owners):,} roster phone numbers name exactly one person")
    branches = branch_index()

    frame["phone_owner"] = [bool(owners.get(p)) for p in frame.act_phone]

    picked = []
    for stratum, block in frame.groupby("stratum"):
        n = PER_EMAIL_CELL if stratum.endswith("|email") else PER_NOEMAIL_CELL
        take = block.sample(min(n, len(block)), random_state=args.seed)
        take = take.assign(draw="random")
        picked.append(take)
        if stratum.endswith("|no-email"):
            pool = block[block.phone_owner & ~block.act_id.isin(take.act_id)]
            extra = pool.sample(min(PHONE_ENRICH_PER_CELL, len(pool)),
                                random_state=args.seed).assign(draw="phone")
            picked.append(extra)
    sample = pd.concat(picked)

    anchor_rows = []
    for crd, name, wanted, why in ANCHORS:
        hit = frame[(frame.advisor_crd == crd) & (frame.name == name)]
        if not hit.empty:
            anchor_rows.append(hit.iloc[[0]].assign(draw="anchor"))
    sample = pd.concat([sample] + anchor_rows)
    sample = sample.drop_duplicates(subset=["act_id", "advisor_crd"], keep="last")

    labels, channels, evidence = [], [], []
    for r in sample.itertuples(index=False):
        lab, ch, ev = label_row(r, owners, branches)
        labels.append(lab)
        channels.append(ch)
        evidence.append(ev)
    sample["label"] = labels
    sample["evidence_channel"] = channels
    sample["evidence"] = evidence

    # Stratum populations travel with the file: without N and n, a stratified
    # sample cannot be turned back into a population rate, and somebody will
    # read the raw counts as if it were random.
    pops = frame.stratum.value_counts().to_dict()
    sample["stratum_population"] = sample.stratum.map(pops)
    drawn = sample[sample.draw == "random"].stratum.value_counts().to_dict()
    sample["stratum_random_drawn"] = sample.stratum.map(drawn).fillna(0).astype(int)

    cols = ["act_id", "advisor_crd", "stratum", "draw", "label",
            "evidence_channel", "evidence", "name", "first_name", "middle_name",
            "last_name", "used_first_name", "email", "act_phone", "act_line1",
            "act_city", "act_addr_state", "tier_pre", "match_score", "namesakes",
            "company", "stratum_population", "stratum_random_drawn"]
    sample[cols].sort_values(["stratum", "draw", "act_id"]).to_csv(
        OUT, index=False, encoding="utf-8")
    print(f"[*] wrote {len(sample):,} rows to {OUT}")

    print("\nLABELS BY STRATUM  (population / drawn / labelled)")
    grid = sample.pivot_table(index="stratum", columns="label", values="act_id",
                              aggfunc="count", fill_value=0)
    grid["population"] = grid.index.map(pops)
    print(grid.to_string())
    print("\nby draw:")
    print(sample.pivot_table(index="draw", columns="label", values="act_id",
                             aggfunc="count", fill_value=0).to_string())
    print("\nby evidence channel:")
    print(sample.evidence_channel.replace("", "none (UNKNOWN)").value_counts().to_string())
    dec = sample[sample.label != "UNKNOWN"]
    print(f"\n[*] {len(dec):,} of {len(sample):,} rows carry a label "
          f"({len(sample) - len(dec):,} UNKNOWN, "
          f"{(len(sample) - len(dec)) / len(sample):.1%})")


if __name__ == "__main__":
    main()
