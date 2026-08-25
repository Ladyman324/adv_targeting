"""Measure the Act!->SEC identity gate instead of arguing about it.

WHY THIS EXISTS
---------------
Four candidate versions of the first-name gate in src/act_crosswalk.py produced
four different demotion counts -- 721, 2,333, 816, 1,126 -- and there was no way
to say which was closest to right, because "closest to right" had no referent.
A count is not a result. Two rules can demote the same number of rows and
disagree about every row in it.

So this file does two things and nothing else:

  * runs ANY candidate gate over ALL matched crosswalk rows and reports what it
    demotes, including its verdict on the hand-verified anchor cases;
  * scores that gate against data/interim/gate_truth.csv -- the labelled sample
    built by scripts/gate_label.py from evidence that is INDEPENDENT of the Act!
    name the gate is judging -- and names the rows it gets wrong.

IT CHANGES NOTHING. src/act_crosswalk.py is not imported, not edited and not
run. The rules below are PROPOSALS; the one that wins gets applied by a human.

Run:
  python scripts/gate_eval.py                    # every rule, all matched rows
  python scripts/gate_eval.py --rules a,d        # just these
  python scripts/gate_eval.py --errors d         # list the rows rule d gets wrong
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import pathlib
import re
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from nicknames import same_person                                   # noqa: E402

INTERIM = ROOT / "data" / "interim"
RAW = ROOT / "data" / "raw"
CROSSWALK = INTERIM / "act_crosswalk.parquet"
ADVISORS = INTERIM / "advisors.parquet"
BRANCHES = INTERIM / "advisor_branches.parquet"
TRUTH = INTERIM / "gate_truth.csv"

# The eight cases verified by hand against advisors.parquet, plus what the
# verifier said the gate must do with them. An anchor is not a score -- eight
# rows cannot measure a rule over 41,594 -- but a rule that fails one of these
# is wrong in a way that is already known, and it should have to say so out loud.
ANCHORS = [
    ("843291",  "Marlyn Campbell",  "demote", "SEC WAYNE CAMPBELL"),
    ("6426473", "Dave Harris",      "demote", "SEC Shanicia Harris"),
    ("7601570", "Jason Main",       "demote", "SEC Marissa Main"),
    ("2387916", "James Nowakowski", "keep",   "SEC KEVIN JAMES NOWAKOWSKI (middle name)"),
    ("2052211", "Raymond Mones",    "keep",   "SEC RAYMOND ALEXANDER MONES"),
    ("1806841", "Brandt Haring",    "keep",   "SEC DAVID BRANDT HARING (middle name)"),
    ("6838725", "AJ Gallego",       "keep",   "SEC Alexander Joseph Gallego (initials)"),
    ("1267466", "Hank Rottenberg",  "keep",   "SEC HERBERT ROTTENBERG (nickname)"),
]

GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "aol.com", "hotmail.com", "outlook.com",
    "comcast.net", "msn.com", "icloud.com", "me.com", "att.net", "mac.com",
    "verizon.net", "sbcglobal.net", "bellsouth.net", "live.com", "ymail.com",
    "earthlink.net", "cox.net", "charter.net", "protonmail.com", "juno.com",
}

# Local parts that name a desk rather than a person. A mailbox called `info` or
# `clientservices` is evidence about a firm, never about which human this row is.
ROLE_TOKENS = {
    "info", "contact", "service", "services", "clientservice", "clientservices",
    "team", "admin", "office", "sales", "support", "client", "clients",
    "advisor", "advisors", "wealth", "group", "help", "inquiries", "main",
    "reception", "hello", "mail", "email", "newaccounts", "operations", "ops",
}


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def _norm(value) -> str:
    return re.sub(r"[^a-z]", "", str(value or "").lower())


def act_contact_fields() -> pd.DataFrame:
    """The Act! fields the gate never looks at: email, phone, business address.

    Read straight from the API pull rather than from act_crosswalk.parquet,
    because the crosswalk carries only what the MATCHER needed. The address and
    the phone are the two witnesses in an Act! record that owe nothing to the
    name typed in it, and they are the reason a row can be labelled at all when
    the email is missing.
    """
    files = sorted(glob.glob(str(RAW / "act_contacts_*.json")))
    if not files:
        raise SystemExit("No Act! pull in data/raw/act_contacts_*.json")
    rows = json.loads(pathlib.Path(files[-1]).read_text(encoding="utf-8"))
    out = []
    for r in rows:
        addr = r.get("businessAddress") or {}
        phone = re.sub(r"\D", "", str(r.get("businessPhone") or ""))
        if len(phone) == 11 and phone.startswith("1"):
            phone = phone[1:]
        out.append({
            "act_id": str(r.get("id") or ""),
            "act_phone": phone if len(phone) == 10 else "",
            "act_ext": str(r.get("businessExtension") or "").strip(),
            "act_line1": str(addr.get("line1") or "").strip(),
            "act_city": str(addr.get("city") or "").strip(),
            "act_addr_state": str(addr.get("state") or "").strip().upper(),
            "act_postal": str(addr.get("postalCode") or "").strip()[:5],
            "act_alt_email": str(r.get("altEmailAddress") or "").strip().lower(),
        })
    return pd.DataFrame(out)


def load() -> pd.DataFrame:
    """Matched crosswalk rows, joined to the SEC filing and the Act! fields.

    `tier_pre` restores the tier the matcher assigned BEFORE the gate ran, which
    is the only tier a candidate gate can be asked about: today's file already
    shows the current gate's 1,126 demotions as `review`, and evaluating a new
    rule against a column the old rule has already edited measures nothing.
    """
    walk = pd.read_parquet(CROSSWALK)
    walk = walk[walk["advisor_crd"].astype(str) != ""].copy()
    walk["advisor_crd"] = walk["advisor_crd"].astype(str)
    walk["demoted"] = walk["demoted"].fillna("")
    walk["tier_pre"] = [
        "high" if (t == "high" or d) else t
        for t, d in zip(walk["tier"], walk["demoted"])
    ]

    filing = pd.read_parquet(ADVISORS, columns=[
        "advisor_crd", "first_name", "middle_name", "last_name", "suffix",
        "used_first_name"])
    filing["advisor_crd"] = filing["advisor_crd"].astype(str)
    filing = filing.drop_duplicates("advisor_crd")
    walk = walk.merge(filing, on="advisor_crd", how="left")

    walk = walk.merge(act_contact_fields(), on="act_id", how="left")
    for col in ("act_phone", "act_line1", "act_city", "act_postal",
                "act_addr_state", "act_alt_email", "act_ext"):
        walk[col] = walk[col].fillna("")
    walk["email"] = walk["email"].fillna("")
    walk["name"] = walk["name"].fillna("")
    return walk


def branch_index() -> dict:
    """crd -> the set of (street, postal) its SEC branches are filed at."""
    frame = pd.read_parquet(BRANCHES)
    frame["advisor_crd"] = frame["advisor_crd"].astype(str)
    out = collections.defaultdict(set)
    for r in frame.itertuples(index=False):
        out[r.advisor_crd].add((
            _norm_street(r.branch_street1),
            str(r.branch_postal or "").strip()[:5],
            _norm(r.branch_city),
        ))
    return out


_STREET_WORDS = {
    "street": "st", "st": "st", "avenue": "ave", "ave": "ave", "road": "rd",
    "rd": "rd", "drive": "dr", "dr": "dr", "boulevard": "blvd", "blvd": "blvd",
    "suite": "ste", "ste": "ste", "north": "n", "south": "s", "east": "e",
    "west": "w", "parkway": "pkwy", "pkwy": "pkwy", "lane": "ln", "ln": "ln",
    "highway": "hwy", "hwy": "hwy", "court": "ct", "circle": "cir",
    "place": "pl", "floor": "fl", "fl": "fl",
}


def _norm_street(value) -> str:
    text = re.sub(r"[^a-z0-9 ]", " ", str(value or "").lower())
    parts = [_STREET_WORDS.get(p, p) for p in text.split()]
    # The suite is dropped: the same advisor's address is filed with and without
    # it, and "1 Main St Ste 200" and "1 Main St" are the same office.
    if "ste" in parts:
        parts = parts[:parts.index("ste")]
    return " ".join(parts).strip()


# --------------------------------------------------------------------------
# name evidence
# --------------------------------------------------------------------------

def act_givens(name: str) -> list:
    """Every given name in an Act! name string, surname dropped."""
    text = re.sub(r"^(mr|mrs|ms|dr|miss|rev|sir)\.?\s+", "", str(name or "").strip(),
                  flags=re.I)
    text = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", " ", text, flags=re.I)
    parts = [_norm(p) for p in re.split(r"[\s.,]+", text)]
    parts = [p for p in parts if p]
    return parts[:-1] if len(parts) > 1 else parts


def _dedup(seq) -> list:
    """Order preserved, duplicates dropped.

    ORDER IS LOAD-BEARING and a set destroyed it. initials_agree() reads the
    filed given names in sequence -- "AJ" is A then J across Alexander Joseph --
    so feeding it a set made the answer depend on Python's hash seed, and two
    runs of this file over the same data disagreed about two rows. Anything the
    initialism test can see is an ordered list from here on.
    """
    out = []
    for item in seq:
        if item and item not in out:
            out.append(item)
    return out


def filed_raw(row) -> list:
    """The token set today's gate uses: first_name and middle_name, UNSPLIT.

    Unsplit is not a detail. src/act_crosswalk.py builds this set from the two
    fields as whole strings, and same_person() then keeps only the first word of
    each -- so a filing of "KEVIN MICHAEL" contributes `kevin` and silently
    discards `michael`. 39 rows are demoted today for exactly that.
    """
    return _dedup(str(t).strip().lower() for t in (row.first_name, row.middle_name)
                  if t and str(t).strip())


def filed_split(row) -> list:
    """Every given name the SEC filing carries, including the USED name.

    used_first_name is not a CRM field and does not come from Act!. It is parsed
    by src/parse_advisors.py out of the individual's own Form U4 <OthrNms>, so
    it is the regulator's record of what this person is called: CRD 1267466
    files HERBERT and also files HANK, CRD 6838725 files Alexander Joseph and
    also files AJ.
    """
    toks = []
    for field in (row.first_name, row.middle_name, row.used_first_name):
        for part in re.split(r"[\s.,]+", str(field or "")):
            part = part.strip().lower()
            # "nan" and "none" are what a missing middle name stringifies to.
            # Today's gate carries them into its token set as literal words --
            # harmless in practice, kept out of the proposal on principle.
            if part and part not in ("none", "nan"):
                toks.append(part)
    return _dedup(toks)


def initials_agree(name: str, tokens) -> bool:
    """Does an Act! given name spell out the SEC given names as initials?

    "AJ Gallego" against Alexander Joseph Gallego. same_person() cannot reach
    this: it compares one string to one string, and `aj` is neither a nickname
    for `alexander` nor a truncation of it nor a bare initial. Two or three
    letters standing for two or three filed given names in order is a specific,
    checkable claim, and it is how a large number of advisors are addressed.

    Runs in both directions, because the initialism can be on either side --
    Act! "AJ Gallego" against filed "Alexander Joseph", or Act! "Alexander
    Joseph Gallego" against a filed used name of "AJ".
    """
    filed = [t for t in (_norm(t) for t in tokens) if t]
    given = [g for g in act_givens(name) if g]
    if not filed or not given:
        return False

    def is_initialism(short: str, longs: list) -> bool:
        if not (2 <= len(short) <= 3):
            return False
        if len(longs) < len(short):
            return False
        # In order, first letters, allowing filed names to be skipped over:
        # "AJ" against [alexander, joseph, gallego] consumes A then J.
        i = 0
        for ch in short:
            while i < len(longs) and longs[i][0] != ch:
                i += 1
            if i >= len(longs):
                return False
            i += 1
        return True

    for g in given:
        if is_initialism(g, filed):
            return True
    for f in filed:
        if is_initialism(f, given):
            return True
    return False


def email_parts(row):
    """(given, surname_seen, domain, how) from the Act! email local part.

    The local part is the firm's own statement of who owns the mailbox, and it
    is independent of the name typed into the Act! name field -- which is the
    whole point, because the name field is what is on trial.

    Returns given="" when the local part cannot be read as this person's name:
    a role mailbox, a surname with nothing else, or a local part in which the
    filed surname does not appear at all (a shared team address, a maiden name,
    a mailbox we cannot attribute). Silence, not a guess.
    """
    email = str(row.email or "").strip().lower()
    if not email or "@" not in email:
        return "", False, "", "no email"
    local, _, domain = email.partition("@")
    last = _norm(row.last_name)
    local = re.sub(r"\d+", "", local)
    toks = [t for t in (_norm(p) for p in re.split(r"[._\-+]", local)) if t]
    if not toks:
        return "", False, domain, "unreadable local part"
    if any(t in ROLE_TOKENS for t in toks) and last not in toks:
        return "", False, domain, "role mailbox"
    if last and last in toks:
        others = [t for t in toks if t != last]
        if not others:
            return "", True, domain, "surname only"
        return max(others, key=len), True, domain, "local part is given+surname"
    if len(toks) == 1 and last:
        one = toks[0]
        if one.endswith(last) and len(one) > len(last):
            return one[:-len(last)], True, domain, "local part is givensurname"
        if one.startswith(last) and len(one) > len(last):
            return one[len(last):], True, domain, "local part is surnamegiven"
    return "", False, domain, "filed surname not in local part"


# --------------------------------------------------------------------------
# the candidate gates
# --------------------------------------------------------------------------
# Each returns True for AGREES (keep in high) and False for DISAGREES (demote).
# They take the joined row, so they may look at anything -- but what each one is
# ALLOWED to look at is the whole point of comparing them, and is stated below.

def rule_a(row) -> bool:
    """(a) TODAY'S GATE. first_name/middle_name as filed, unsplit, nicknames on."""
    toks = filed_raw(row)
    if not toks:
        return True                        # no filing evidence: do not demote
    return any(same_person(row.name, t) for t in toks)


def rule_b(row) -> bool:
    """(b) today's gate + the initials rule."""
    toks = filed_raw(row)
    if not toks:
        return True
    if any(same_person(row.name, t) for t in toks):
        return True
    return initials_agree(row.name, toks)


def rule_c(row) -> bool:
    """(c) (b) + email local-part corroboration.

    A disagreement is forgiven when the mailbox itself agrees with the SEC
    filing. This is a claim about the ROW, not about the name: if the contact's
    firm-issued address is wayne.campbell@lpl.com and the filing says WAYNE
    CAMPBELL, the row is Wayne Campbell's row whatever the name field says.

    Only a firm domain counts. A gmail local part is chosen by its owner and can
    say anything.
    """
    if rule_b(row):
        return True
    given, has_surname, domain, _ = email_parts(row)
    if not given or not has_surname or domain in GENERIC_DOMAINS:
        return False
    toks = filed_split(row)
    if any(same_person(given, t) for t in toks):
        return True
    return initials_agree(given + " " + str(row.last_name), toks)


def rule_d(row) -> bool:
    """(d) THE PROPOSAL: filing split into tokens, USED name included, initials.

    Three changes to (a), each fixing a demotion the SEC's own file says is
    wrong:

      * split the name fields on whitespace, so a filing of "KEVIN MICHAEL"
        offers both given names instead of only the first;
      * include used_first_name, the name the advisor told the regulator they
        go by -- HANK for HERBERT ROTTENBERG, AJ for Alexander Gallego;
      * accept an initialism, which no nickname table can ever hold.

    Still the filing and only the filing. No email, no CRM, nothing Act! could
    have supplied to itself.
    """
    toks = filed_split(row)
    if not toks:
        return True
    if any(same_person(row.name, t) for t in toks):
        return True
    return initials_agree(row.name, toks)


def rule_e(row) -> bool:
    """(e) (d) + the email as a second witness, in BOTH directions.

    Forgives a name disagreement the mailbox settles, and -- unlike every other
    rule here -- demotes a row whose names agree but whose firm-issued mailbox
    belongs to a different person of the same surname. That case is invisible to
    any name-only gate and is exactly the harm the gate exists to stop.
    """
    given, has_surname, domain, _ = email_parts(row)
    usable = bool(given) and has_surname and domain not in GENERIC_DOMAINS
    toks = filed_split(row)
    if rule_d(row):
        if usable and len(given) > 2 and toks:
            agrees = (any(same_person(given, t) for t in toks)
                      or initials_agree(given + " " + str(row.last_name), toks))
            if not agrees:
                return False               # the mailbox says somebody else
        return True
    if usable and toks:
        if (any(same_person(given, t) for t in toks)
                or initials_agree(given + " " + str(row.last_name), toks)):
            return True
    return False


def filed_raw_used(row) -> list:
    """first_name, middle_name AND used_first_name, each unsplit."""
    return _dedup(str(t).strip().lower()
                  for t in (row.first_name, row.middle_name, row.used_first_name)
                  if t and str(t).strip())


def rule_f(row) -> bool:
    """(f) THE GATE AS IT NOW STANDS IN src/act_crosswalk.py.

    src/act_crosswalk.py was edited by another session WHILE this harness was
    being written: at the start of the session it demoted 1,126 rows on
    first_name and middle_name (that is rule (a)); it now demotes 851, having
    added used_first_name to the same unsplit comparison. Rule (a) is kept
    because it is the rule the four disputed counts came from and the rule the
    anchors were verified against; this is what is actually on disk.

    It differs from the proposal (d) in two ways only: it does not split a name
    field on whitespace, and it has no initials rule.
    """
    toks = filed_raw_used(row)
    if not toks:
        return True
    return any(same_person(row.name, t) for t in toks)


RULES = {
    "a": (rule_a, "session-start gate: filing first/middle, unsplit"),
    "b": (rule_b, "(a) + initials"),
    "c": (rule_c, "(b) + email local-part corroboration"),
    "d": (rule_d, "filing split + used_first_name + initials"),
    "e": (rule_e, "(d) + email as a two-way second witness"),
    "f": (rule_f, "the gate now on disk: filing first/middle/used, unsplit"),
}


def apply_rule(frame: pd.DataFrame, key: str) -> pd.Series:
    fn = RULES[key][0]
    return pd.Series([fn(r) for r in frame.itertuples(index=False)],
                     index=frame.index)


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def anchor_table(frame: pd.DataFrame, keys: list) -> None:
    print("\nANCHORS  (verified by hand against advisors.parquet)")
    header = f"{'crd':>8}  {'act name':22s} {'wanted':7s}"
    print(header + "  " + "  ".join(f"{k:^7s}" for k in keys))
    misses = collections.Counter()
    for crd, name, wanted, _why in ANCHORS:
        hit = frame[(frame.advisor_crd == crd) & (frame.name == name)]
        if hit.empty:
            print(f"{crd:>8}  {name:22s} {wanted:7s}  ROW NOT IN CROSSWALK")
            continue
        # itertuples, NOT .iloc[0]: on a Series `row.name` is the index label,
        # so every rule would silently judge an integer instead of the Act! name
        # and every anchor would pass.
        row = next(hit.itertuples(index=False))
        cells = []
        for k in keys:
            got = "keep" if RULES[k][0](row) else "demote"
            ok = got == wanted
            misses[k] += 0 if ok else 1
            cells.append(f"{got:^7s}" if ok else f"{got.upper():^7s}")
        print(f"{crd:>8}  {name:22s} {wanted:7s}  " + "  ".join(cells))
    print("  " + " " * 38 + "  ".join(f"{misses[k]:^7d}" for k in keys)
          + "   <- anchors failed (CAPS above)")


def counts_table(frame: pd.DataFrame, verdicts: dict, keys: list) -> None:
    high = frame.tier_pre == "high"
    print(f"\nDEMOTIONS over {len(frame):,} matched rows "
          f"({int(high.sum()):,} of them high-tier before any gate)")
    print(f"  {'rule':4s} {'high demoted':>13s} {'% of high':>10s}  "
          f"{'all rows disagreeing':>21s}  description")
    for k in keys:
        v = verdicts[k]
        dem_high = int((~v & high).sum())
        dem_all = int((~v).sum())
        print(f"  {k:4s} {dem_high:>13,} {dem_high / max(int(high.sum()), 1):>9.1%}  "
              f"{dem_all:>21,}  {RULES[k][1]}")
    print(f"  {'--':4s} {int((frame.demoted != '').sum()):>13,} "
          f"{'':>10s}  {'':>21s}  as recorded in act_crosswalk.parquet today")


def pairwise(frame: pd.DataFrame, verdicts: dict, keys: list) -> None:
    """Two rules can demote similar counts and disagree about the rows."""
    high = frame.tier_pre == "high"
    print("\nWHERE THE RULES DISAGREE WITH EACH OTHER (high-tier rows)")
    print("       " + "  ".join(f"{k:>6s}" for k in keys))
    for a in keys:
        cells = []
        for b in keys:
            cells.append(f"{int((verdicts[a] != verdicts[b])[high].sum()):>6,}")
        print(f"  {a:4s} " + "  ".join(cells))


def score_against_truth(frame: pd.DataFrame, verdicts: dict, keys: list,
                        errors_for: str = "") -> None:
    if not TRUTH.exists():
        print(f"\n[!] {TRUTH} not built yet -- run scripts/gate_label.py first. "
              f"Counts above are counts, not correctness.")
        return
    truth = pd.read_csv(TRUTH, dtype=str)
    truth = truth[truth.label.isin(["SAME", "DIFFERENT"])]
    joined = frame.merge(truth[["act_id", "advisor_crd", "label", "draw",
                                "evidence_channel", "evidence"]],
                         on=["act_id", "advisor_crd"], how="inner")
    print(f"\nAGAINST data/interim/gate_truth.csv: {len(joined):,} labelled rows "
          f"({int((joined.label == 'DIFFERENT').sum()):,} DIFFERENT, "
          f"{int((joined.label == 'SAME').sum()):,} SAME)")

    for scope, sub in (("all labelled rows", joined),
                       ("random draw only", joined[joined.draw == "random"]),
                       ("phone-labelled only (no email used to label)",
                        joined[joined.evidence_channel == "phone"])):
        if sub.empty:
            continue
        print(f"\n  -- {scope}: n={len(sub):,}")
        print(f"     {'rule':4s} {'TP':>5s} {'FP':>5s} {'FN':>5s} {'TN':>5s} "
              f"{'precision':>10s} {'recall':>8s} {'F1':>6s} {'accuracy':>9s}")
        for k in keys:
            v = apply_rule(sub, k)
            demote = ~v
            diff = sub.label == "DIFFERENT"
            tp = int((demote & diff).sum())
            fp = int((demote & ~diff).sum())
            fn = int((~demote & diff).sum())
            tn = int((~demote & ~diff).sum())
            prec = tp / (tp + fp) if tp + fp else float("nan")
            rec = tp / (tp + fn) if tp + fn else float("nan")
            f1 = (2 * prec * rec / (prec + rec)) if prec and rec and prec + rec else float("nan")
            acc = (tp + tn) / len(sub)
            print(f"     {k:4s} {tp:>5d} {fp:>5d} {fn:>5d} {tn:>5d} "
                  f"{prec:>10.3f} {rec:>8.3f} {f1:>6.3f} {acc:>9.3f}")

    if errors_for:
        for k in errors_for.split(","):
            v = apply_rule(joined, k)
            bad = joined[(~v & (joined.label == "SAME"))
                         | (v & (joined.label == "DIFFERENT"))]
            print(f"\n  ROWS RULE {k} GETS WRONG ({len(bad)}):")
            for r in bad.itertuples(index=False):
                kind = "FALSE DEMOTION" if r.label == "SAME" else "MISSED MISMATCH"
                print(f"    {kind:15s} crd {r.advisor_crd:>8s}  act '{r.name}'  "
                      f"SEC {r.first_name} {r.middle_name or ''} {r.last_name} "
                      f"(used {r.used_first_name or '-'})")
                print(f"                    evidence: {r.evidence}")


def census(frame: pd.DataFrame, keys: list, errors_for: str = "") -> None:
    """The same evidence test, run over every matched row instead of a sample.

    The labelled sample is the deliverable and the honest estimate: it is drawn
    at random inside its strata, so its rates mean something. This is the
    complement -- the evidence rule applied exhaustively, which cannot be biased
    by a draw and turns "no such row appeared in 60" into an exact count.

    Same labels, same silence: a row the email and the phone cannot settle stays
    UNKNOWN here too, and only the settled ones are scored.
    """
    from gate_label import label_row, roster_phone_owners            # noqa: E402

    owners = roster_phone_owners()
    branches = branch_index()
    labels = [label_row(r, owners, branches)[0]
              for r in frame.itertuples(index=False)]
    frame = frame.assign(label=labels)
    dec = frame[frame.label != "UNKNOWN"]
    print(f"\nCENSUS: the same evidence over all {len(frame):,} matched rows -- "
          f"{len(dec):,} settled ({int((dec.label == 'DIFFERENT').sum()):,} "
          f"DIFFERENT, {int((dec.label == 'SAME').sum()):,} SAME), "
          f"{len(frame) - len(dec):,} UNKNOWN "
          f"({(len(frame) - len(dec)) / len(frame):.1%})")
    print(f"  {'rule':4s} {'TP':>6s} {'FP':>6s} {'FN':>6s} {'TN':>7s} "
          f"{'precision':>10s} {'recall':>8s} {'F1':>6s}")
    for k in keys:
        v = apply_rule(dec, k)
        demote, diff = ~v, dec.label == "DIFFERENT"
        tp = int((demote & diff).sum()); fp = int((demote & ~diff).sum())
        fn = int((~demote & diff).sum()); tn = int((~demote & ~diff).sum())
        prec = tp / (tp + fp) if tp + fp else float("nan")
        rec = tp / (tp + fn) if tp + fn else float("nan")
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else float("nan")
        print(f"  {k:4s} {tp:>6,} {fp:>6,} {fn:>6,} {tn:>7,} "
              f"{prec:>10.3f} {rec:>8.3f} {f1:>6.3f}")
    for k in [x for x in errors_for.split(",") if x in RULES]:
        v = apply_rule(dec, k)
        bad = dec[(~v & (dec.label == "SAME")) | (v & (dec.label == "DIFFERENT"))]
        print(f"\n  CENSUS ROWS RULE {k} GETS WRONG ({len(bad):,}) -- first 25:")
        for r in list(bad.itertuples(index=False))[:25]:
            kind = "FALSE DEMOTION" if r.label == "SAME" else "MISSED MISMATCH"
            print(f"    {kind:15s} crd {r.advisor_crd:>8s}  act '{r.name}'  "
                  f"email {r.email}  SEC {r.first_name} {r.middle_name or ''} "
                  f"{r.last_name} (used {r.used_first_name or '-'})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rules", default=",".join(RULES),
                    help="comma-separated rule keys, default all")
    ap.add_argument("--errors", default="",
                    help="rule keys whose mislabelled rows should be printed")
    ap.add_argument("--census", action="store_true",
                    help="also run the evidence test over every matched row")
    args = ap.parse_args()
    keys = [k.strip() for k in args.rules.split(",") if k.strip() in RULES]

    frame = load()
    verdicts = {k: apply_rule(frame, k) for k in keys}
    counts_table(frame, verdicts, keys)
    pairwise(frame, verdicts, keys)
    anchor_table(frame, keys)
    score_against_truth(frame, verdicts, keys, args.errors)
    if args.census:
        census(frame, keys, args.errors)


if __name__ == "__main__":
    main()
