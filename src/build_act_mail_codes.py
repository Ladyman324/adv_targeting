"""Extract the do-not-email population from an Act! contact export.

WHY THIS EXISTS
---------------
Act! carries a "Mail Code" picklist (customFields.email__y_n) whose values decide
whether a contact may be mailed at all:

    1  Email and hard copy    3  Hard Copy Only     P  Prospect, no mass mailings
    2  Email only             C  Client             N  No mail; cannot locate; bounce backs
    NC No mail by request      U  UNSUBSCRIBE       BB (off-picklist, in practice bounce-back)

The application's own suppression list only knows about people who clicked the
preference link in one of OUR emails. It knew nothing about the years of opt-outs
already recorded in the CRM -- so on the 2026-08-13 export, 1,560 addresses that
Act! marks do-not-email were still selectable and sendable in the app, including
701 explicit UNSUBSCRIBEs. This file closes that gap before the first real send.

The output ships inside the API so the check needs no import step and no table
writes: it is a floor that applies from the moment it deploys. The live
suppression table is still consulted as well, and takes precedence for anything
newer.

Re-run this whenever a fresh export lands.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "api" / "shared" / "act_mail_codes.json"

# Codes that mean "do not email". Deliberately INCLUSIVE: N is "cannot locate /
# bounce backs", which is a deliverability fact rather than a preference, but
# mailing a known-bad address repeatedly is how a sending domain earns a spam
# reputation. BB is off-picklist and appears to be a bounce-back marker; treated
# the same way, because the cost of wrongly suppressing a prospect is one missed
# email while the cost of wrongly mailing an opt-out is a compliance problem.
NO_EMAIL = {"U", "N", "NC", "BB"}
FIELD = "email__y_n"
ADDRESS = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def objects(fh):
    """Stream top-level objects from a JSON array. The export is ~285 MB."""
    buf, depth, instr, esc, started = [], 0, False, False, False
    while True:
        chunk = fh.read(1 << 22)
        if not chunk:
            return
        for ch in chunk:
            if not started:
                if ch == "{":
                    started, depth, buf = True, 1, ["{"]
                continue
            buf.append(ch)
            if instr:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    instr = False
                continue
            if ch == '"':
                instr = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield json.loads("".join(buf))
                    started = False


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(f"usage: {pathlib.Path(sys.argv[0]).name} <act_contacts_export.json>")
    src = pathlib.Path(sys.argv[1])
    if not src.exists():
        raise SystemExit(f"[!] no such export: {src}")

    codes: dict[str, str] = {}
    by_guid: dict[str, str] = {}
    seen = Counter()
    total = 0
    with src.open(encoding="utf-8") as fh:
        for rec in objects(fh):
            total += 1
            cf = rec.get("customFields") or {}
            code = str(cf.get(FIELD) or "").strip().upper()
            seen[code or "(blank)"] += 1
            if code not in NO_EMAIL:
                continue
            # Keyed by contact GUID as well as by address. Act! users routinely
            # overwrite the email field with a note -- "unsubscribed 3/27/26",
            # "retired" -- which destroys the address but not the opt-out. On the
            # 2026-08-13 export that pattern hid 823 people who were still
            # reachable through the address WE hold from SEC data. Matching the
            # contact itself catches them.
            guid = str(rec.get("id") or "").strip().lower()
            if guid and by_guid.get(guid) != "U":
                by_guid[guid] = code
            # Every address on the record, not just the primary: a contact whose
            # opt-out is recorded once should not be reachable through their
            # alternate address.
            for value in (rec.get("emailAddress"), rec.get("altEmailAddress"),
                          rec.get("personalEmailAddress"), cf.get("email_2_email")):
                if isinstance(value, str) and ADDRESS.match(value.strip()):
                    address = value.strip().lower()
                    # Strongest code wins if two records disagree.
                    if codes.get(address) != "U":
                        codes[address] = code

    # GUID -> CRD, via the same crosswalk the call logging trusts.
    xwalk_path = ROOT / "api" / "shared" / "act_contacts.json"
    crds: dict[str, str] = {}
    if xwalk_path.exists():
        xwalk = json.loads(xwalk_path.read_text(encoding="utf-8")).get("contacts", {})
        guid_to_crd = {str(g).lower(): crd for crd, g in xwalk.items()}
        for guid, code in by_guid.items():
            crd = guid_to_crd.get(guid)
            if crd and crds.get(crd) != "U":
                crds[crd] = code
    else:
        print(f"[!] {xwalk_path.name} missing -- CRD matching will be unavailable")

    payload = {
        "built_utc": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(timespec="seconds"),
        "source": src.name,
        "field": f"customFields.{FIELD}",
        "codes_treated_as_no_email": sorted(NO_EMAIL),
        "note": "Do-not-email addresses from the Act! Mail Code field. Consulted by "
                "api/shared/email-suppress.js in addition to the live suppression table.",
        "addresses": dict(sorted(codes.items())),
        "crds": dict(sorted(crds.items())),
    }
    OUT.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    print(f"[*] {total:,} contacts read from {src.name}")
    print("[*] Mail Code distribution:")
    for value, n in seen.most_common(12):
        print(f"      {value:<10} {n:>7,}")
    print(f"[*] written to {OUT.relative_to(ROOT)}")
    print(f"      {len(codes):,} addresses")
    for value, n in Counter(codes.values()).most_common():
        print(f"        {value:<4} {n:>6,}")
    print(f"      {len(crds):,} CRDs")
    for value, n in Counter(crds.values()).most_common():
        print(f"        {value:<4} {n:>6,}")


if __name__ == "__main__":
    main()
