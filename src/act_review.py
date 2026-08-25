"""The questionable Act! -> CRD matches, triaged into what a person must decide.

WHY THIS IS A PIPELINE STEP AND NOT A ONE-OFF
---------------------------------------------
The gate in act_crosswalk.py was rewritten five times in one afternoon, each
version producing a different demotion count and a plausible story, because
nothing scored it. docs/gate_evaluation.md fixed that for the RULE. This fixes
it for the RESIDUE: the matches no rule can settle, which are exactly the ones
where a wrong answer is expensive and invisible.

Left alone, that residue is re-derived and thrown away on every pipeline run --
the same failure act_crosswalk.py exists to prevent for matching itself. So
verdicts are persisted, keyed on the Act! GUID, and re-applied on every build.

THREE PILES, AND ONLY ONE OF THEM IS FOR A HUMAN
------------------------------------------------
  decidable   the email local part names one side and not the other. A rule
              settles these; putting them in front of a reviewer wastes the
              reviewer and invites the judgement errors we are removing.
  review      no decisive email. Address, phone, title, firm and registration
              dates have to be weighed. THIS is the pile to adjudicate.
  contra      the gate KEEPS these at high tier, but the mailbox on the record
              names somebody else. They do not announce themselves and they are
              the ones syncing to Act! today, so they matter most.

A stored verdict carries the evidence that produced it and a hash of the
evidence it was made against. If the underlying data changes, the hash changes
and the row returns to the queue rather than resting on a decision made about
something else.

Run:  python src/act_review.py            triage and report
      python src/act_review.py --queue    write the queue for adjudication
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import pathlib
import re
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
from nicknames import same_person                          # noqa: E402

INTERIM = ROOT / "data" / "interim"
RAW = ROOT / "data" / "raw"
XW = INTERIM / "act_crosswalk.parquet"
VERDICTS = INTERIM / "act_adjudications.csv"
QUEUE = ROOT / "data" / "output" / "act_review_queue.csv"

EVIDENCE_FIELDS = ("act_name", "act_email", "act_phone", "act_street",
                   "sec_first", "sec_middle", "sec_used", "sec_last")


def local_part(email: str) -> str:
    """The mailbox name, or "" when the field is not an address at all.

    1,082 Act! emailAddress values are status notes -- "retired",
    "unsubscribed 7/11/25". Treating those as evidence would be worse than
    having none.
    """
    e = str(email or "").strip().lower()
    if "@" not in e or " " in e:
        return ""
    return e.split("@", 1)[0]


ROLE_TOKENS = {"info", "team", "office", "group", "admin", "sales", "service",
               "contact", "support", "wealth", "advisors", "advisory", "planning"}
GENERIC_DOMAINS = {"gmail.com", "yahoo.com", "aol.com", "hotmail.com", "outlook.com",
                   "comcast.net", "msn.com", "icloud.com", "me.com", "att.net",
                   "mac.com", "verizon.net", "sbcglobal.net", "bellsouth.net",
                   "live.com", "ymail.com", "earthlink.net", "cox.net",
                   "charter.net", "protonmail.com", "juno.com"}


def _norm(v: str) -> str:
    return re.sub(r"[^a-z]", "", str(v or "").lower())


def mailbox_given(email: str, last_name: str):
    """(given, usable) from the local part -- the FIRM's statement of who owns it.

    Returns given="" whenever the mailbox cannot be read as this person's name:
    a role address, a surname alone, or a local part the filed surname does not
    appear in at all. Silence rather than a guess.

    Lifted deliberately from scripts/gate_eval.py:email_parts() rather than
    reinvented -- an earlier version of this file asked a looser question ("is
    the SEC name a substring of the local part") and flagged 1,564 rows, almost
    all of them correct middle-name matches where the mailbox uses the legal
    first name and Act! uses the middle one.
    """
    e = str(email or "").strip().lower()
    if "@" not in e or " " in e:
        return "", False
    local, _, domain = e.partition("@")
    if domain in GENERIC_DOMAINS:
        return "", False
    last = _norm(last_name)
    local = re.sub(r"\d+", "", local)
    toks = [t for t in (_norm(x) for x in re.split(r"[._\-+]", local)) if t]
    if not toks:
        return "", False
    # SUBSTRING, not equality. "wealthmanagementgroup@" arrives as ONE token
    # because there is nothing to split on, so an exact-match test let a team
    # mailbox through and it was reported as an advisor named "Wealth".
    if last not in toks and any(word in t for t in toks for word in ROLE_TOKENS):
        return "", False
    if last and last in toks:
        others = [t for t in toks if t != last]
        return (max(others, key=len), True) if others else ("", False)
    if len(toks) == 1 and last:
        one = toks[0]
        if one.endswith(last) and len(one) > len(last):
            return one[:-len(last)], True
        if one.startswith(last) and len(one) > len(last):
            return one[len(last):], True
    return "", False


def evidence_hash(row: dict) -> str:
    """What a verdict was decided ABOUT, so a later change reopens it."""
    material = "|".join(str(row.get(k, "")) for k in EVIDENCE_FIELDS)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def load_verdicts() -> dict:
    if not VERDICTS.exists():
        return {}
    df = pd.read_csv(VERDICTS, dtype=str).fillna("")
    return {r.act_id: {"verdict": r.verdict, "evidence": r.evidence,
                       "evidence_hash": r.evidence_hash}
            for r in df.itertuples(index=False)}


def sec_givens(rec: dict) -> list:
    toks = []
    for key in ("sec_first", "sec_middle", "sec_used"):
        toks.extend(str(rec.get(key) or "").split())
    return [t.lower() for t in toks if t.strip()]


def disagrees(rec: dict) -> bool:
    toks = sec_givens(rec)
    return bool(toks) and not any(same_person(rec["act_name"], t) for t in toks)


def build() -> pd.DataFrame:
    xw = pd.read_parquet(XW)
    newest = sorted(glob.glob(str(RAW / "act_contacts_*.json")))[-1]
    act = {r.get("id"): r for r in json.loads(pathlib.Path(newest).read_text(encoding="utf-8"))}
    adv = pd.read_parquet(INTERIM / "advisors.parquet",
                          columns=["advisor_crd", "first_name", "middle_name",
                                   "used_first_name", "last_name"])
    sec = {str(r.advisor_crd): r for r in adv.itertuples(index=False)}

    out = []
    for r in xw.itertuples():
        if not str(r.advisor_crd):
            continue
        accused = bool(str(r.demoted))
        if not accused and r.tier != "high":
            continue
        a = act.get(r.act_id) or {}
        s = sec.get(str(r.advisor_crd))
        if s is None:
            continue
        addr = a.get("businessAddress") or {}
        rec = {
            "act_id": r.act_id, "advisor_crd": r.advisor_crd, "tier": r.tier,
            "demoted": r.demoted, "match_score": round(float(r.match_score or 0), 3),
            "act_name": r.name, "act_email": a.get("emailAddress") or "",
            "act_phone": a.get("businessPhone") or "",
            "act_street": addr.get("line1") or "", "act_city": addr.get("city") or "",
            "act_state": addr.get("state") or "", "act_title": a.get("jobTitle") or "",
            "act_company": a.get("company") or "",
            "sec_first": s.first_name or "", "sec_middle": s.middle_name or "",
            "sec_used": s.used_first_name or "", "sec_last": s.last_name or "",
        }
        given, usable = mailbox_given(rec["act_email"], rec["sec_last"])
        names = sec_givens(rec)
        # Does the mailbox owner match the FILING, and does it match Act!?
        mail_is_sec = usable and bool(names) and any(same_person(given, t) for t in names)
        mail_is_act = usable and same_person(given, rec["act_name"])

        if accused and usable and len(given) > 2 and mail_is_sec != mail_is_act:
            rec["pile"] = "decidable"
            rec["signal"] = (f"mailbox '{given}' matches the filing"
                             if mail_is_sec else f"mailbox '{given}' matches the Act! name")
        elif accused:
            rec["pile"] = "review"
            rec["signal"] = "no decisive mailbox"
        elif usable and len(given) > 2 and names and not mail_is_sec:
            # NO name test here, deliberately.
            #
            # This first read `and disagrees(rec)` -- and disagrees() asks the
            # same question the gate just asked. A row only reaches this branch
            # because the gate KEPT it, which means the names agreed, which
            # means disagrees() is False by construction. The pile came back
            # empty and looked like good news.
            #
            # The whole point of this pile is rows where the NAMES agree and the
            # MAILBOX does not. Asking the name question again can only hide
            # them.
            rec["pile"] = "contra"
            rec["signal"] = f"kept at high tier, but mailbox '{given}' matches no filed name"
        else:
            continue
        rec["evidence_hash"] = evidence_hash(rec)
        out.append(rec)
    return pd.DataFrame(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", action="store_true",
                    help="write the rows still needing a decision to data/output/")
    args = ap.parse_args()

    df = build()
    if df.empty:
        print("[*] no questionable matches")
        return
    verdicts = load_verdicts()
    df["settled"] = [
        bool(verdicts.get(r.act_id)
             and verdicts[r.act_id]["evidence_hash"] == r.evidence_hash)
        for r in df.itertuples(index=False)]

    print(f"[*] {len(df):,} questionable matches")
    for pile in ("decidable", "review", "contra"):
        sub = df[df.pile == pile]
        if not len(sub):
            continue
        done = int(sub.settled.sum())
        print(f"      {pile:<10} {len(sub):>6,}   adjudicated {done:,}, "
              f"outstanding {len(sub) - done:,}")

    stale = sum(1 for r in df.itertuples(index=False)
                if verdicts.get(r.act_id)
                and verdicts[r.act_id]["evidence_hash"] != r.evidence_hash)
    if stale:
        print(f"[!] {stale:,} stored verdicts were made against evidence that has "
              f"since changed, so those rows are back in the queue")

    todo = df[(~df.settled) & (df.pile.isin(("review", "contra")))]
    if args.queue:
        QUEUE.parent.mkdir(parents=True, exist_ok=True)
        todo.to_csv(QUEUE, index=False)
        print(f"[*] wrote {len(todo):,} rows to {QUEUE}")
        print("    Adjudicate with evidence, then append act_id, advisor_crd, "
              "verdict, evidence, evidence_hash to")
        print(f"    {VERDICTS}")
    elif len(todo):
        print(f"[!] {len(todo):,} matches still need a decision. "
              f"Run:  python src/act_review.py --queue")
    else:
        print("[*] every questionable match has been adjudicated")


if __name__ == "__main__":
    main()
