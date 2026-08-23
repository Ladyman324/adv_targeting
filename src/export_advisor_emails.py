"""Advisor email -> CRD lookup -> data/output/advisor_emails.json.gz

WHY THIS EXISTS
---------------
The reply sweep reads a rep's mailbox and has to answer one question about every
message it sees: is this from, or to, somebody in our advisor universe? Almost
always the answer is no -- internal mail, newsletters, IT notices -- and only the
yes cases may be persisted at all.

    contacts.json is 42MB. It cannot be loaded inside a Function, per sweep,
    for twenty reps, every fifteen minutes.

So the pipeline emits the one slice the sweep needs. 123k addresses compress to
roughly a megabyte, which a Function loads once per cold start and keeps in a
Map. A negative lookup then costs nothing, which matters because the negative
case is the common one -- a per-message round trip to Table Storage would spend
its whole budget proving that the CFO's mail is not from an advisor.

WHY A BLOB AND NOT A TABLE
--------------------------
Table Storage is a point-lookup store and the sweep's access pattern is the
opposite: many lookups, mostly misses, all against a set that changes only when
the pipeline runs. One blob read per cold start beats one table read per
message by several orders of magnitude, and it makes the swap atomic -- a new
upload either is or is not the file the next cold start sees, with no window
where half the addresses are new and half are old.

AMBIGUOUS ADDRESSES ARE NOT RESOLVED HERE
-----------------------------------------
172 addresses currently map to more than one CRD -- `johnsonc@stifel.com` covers
four different people, because Stifel's scheme is surname-plus-initial and that
is genuinely not unique. Picking one would attribute a real reply to the wrong
advisor, which is worse than not attributing it.

They are published in `ambiguous` instead. The sweep should treat a hit there as
"an advisor wrote to us, we cannot say which" -- real inbound activity, held for
manual linking, never auto-credited to a campaign.

DEFECTS ARE REPORTED, NOT PAPERED OVER
--------------------------------------
Four addresses in the current universe are malformed, and they are scraper bugs
rather than exotic-but-valid addresses:

    jennifer.o&#039;donnell@rbc.com     HTML entity, never unescaped
    steven.d&#039;eredita@rbc.com       same
    ksulliv2<U+200B>@wintrustwealth<U+200B>.com   zero-width spaces, twice
    https://...meet-the-team.htm/matthew@axio...  a URL concatenated with an address

The first three are repaired here, because the repair is deterministic and the
intended address is not in doubt. The fourth is REJECTED: the address after the
last slash is probably right, and "probably" is not good enough for something
that decides whose reply this is. It is printed so it gets fixed at the source.

Run:  python src/export_advisor_emails.py
      python src/export_advisor_emails.py --upload      (needs a credential)
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import hashlib
import html
import json
import os
import pathlib
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTACTS = ROOT / "webapp" / "data" / "contacts.json"
OUT = ROOT / "data" / "output" / "advisor_emails.json.gz"
BLOB_CONTAINER = "lookups"
BLOB_NAME = "advisor_emails.json.gz"

# Invisible characters that survive a copy-paste out of a web page and make an
# address that looks correct in every log and matches nothing at all.
INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)

# Permissive on purpose. This is a shape test to catch scraper wreckage, not an
# RFC 5322 validator -- rejecting a real advisor's unusual address would cost us
# their reply, which is a worse error than admitting an odd-looking one.
SHAPE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# OUR OWN PEOPLE ARE NOT PROSPECTS, AND THEIR MAIL IS NOT ACTIVITY.
#
# 18 advisors in the SEC feed are EIC's own registered reps. Left in this file
# they would be recognised by the reply sweep like any other advisor -- and the
# activity timeline is FIRM-WIDE, so every rep would be able to read when each
# of their colleagues emailed each other. Internal mail is also far more likely
# to be sensitive than anything an advisor sends us.
#
# So they are excluded from every lookup here: the sweep cannot recognise their
# addresses, nothing is recorded, and a follow-up has no address to resolve.
# Their CRDs are published separately as `internalCrds` so the applications can
# SAY that rather than showing an empty timeline that looks like a bug.
#
# Mirrors EMAIL_INTERNAL_DOMAINS in api/shared/email-core.js, and defaults the
# same way. One definition of "internal", two languages.
INTERNAL_DOMAINS = {d.strip().lower() for d in
                    os.environ.get("EMAIL_INTERNAL_DOMAINS", "eicatlanta.com").split(",")
                    if d.strip()}


def is_internal(address: str) -> bool:
    return address.split("@")[-1].lower() in INTERNAL_DOMAINS


def normalise(raw: str) -> tuple[str, str]:
    """(address, repair) -- repair names what had to be fixed, "" if nothing."""
    text = str(raw or "").strip()
    if not text:
        return "", ""
    repairs = []

    cleaned = text.translate(INVISIBLE)
    if cleaned != text:
        repairs.append("zero-width")
        text = cleaned

    if re.search(r"&[a-zA-Z#0-9]{2,6};", text):
        unescaped = html.unescape(text)
        if unescaped != text:
            repairs.append("html-entity")
            text = unescaped

    text = text.lower().strip()
    return text, "+".join(repairs)


def load() -> dict:
    with CONTACTS.open(encoding="utf-8") as fh:
        return json.load(fh)["advisors"]


def build(advisors: dict) -> tuple[dict, list, dict]:
    by_email: dict[str, set] = collections.defaultdict(set)
    by_crd: dict[str, str] = {}
    internal: list = []
    firm_votes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    report = {"rows": 0, "with_email": 0, "repaired": [], "rejected": []}

    for crd, rec in advisors.items():
        report["rows"] += 1
        raw = rec.get("e", "")
        if not str(raw).strip():
            continue
        report["with_email"] += 1
        address, repair = normalise(raw)
        if not SHAPE.match(address):
            report["rejected"].append((crd, raw))
            continue
        if repair:
            report["repaired"].append((crd, raw, address, repair))
        if is_internal(address):
            # Recorded as internal and excluded from every lookup below.
            internal.append(str(crd))
            continue
        by_email[address].add(str(crd))
        # The reverse direction, which is what a FOLLOW-UP needs.
        #
        # Included even when the address is ambiguous in the forward direction:
        # `johnsonc@stifel.com` cannot tell us WHICH Johnson wrote to us, but we
        # still know it is the address on file for each of them, and writing to
        # a named advisor at their own address is not a guess.
        by_crd[str(crd)] = address
        firm = str(rec.get("fc", "") or "").strip()
        if firm:
            firm_votes[address.split("@")[-1]][firm] += 1

    resolved = {e: sorted(c)[0] for e, c in by_email.items() if len(c) == 1}
    ambiguous = sorted(e for e, c in by_email.items() if len(c) > 1)

    # Domain -> firm CRD, for the case the plan calls an accepted gap: an advisor
    # replying from an address we do not hold. It cannot name the person, but a
    # message from an unknown ubs.com address is still worth recognising as
    # advisor traffic rather than discarding as noise.
    #
    # Only domains that point overwhelmingly at ONE firm are kept. A domain split
    # across firms is an aggregator or a free provider and names nobody -- the
    # same rule derive_domain_map() applies in build_contacts.py.
    by_domain = {}
    for domain, votes in firm_votes.items():
        firm, n = votes.most_common(1)[0]
        if len(votes) == 1 and n >= 2:
            by_domain[domain] = firm

    return resolved, ambiguous, {**report, "by_domain": by_domain, "by_crd": by_crd,
                                 "internal": sorted(set(internal))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--upload", action="store_true",
                    help="also upload to blob storage (reads AZURE_STORAGE_CONNECTION_STRING)")
    args = ap.parse_args()

    if not CONTACTS.exists():
        print(f"missing {CONTACTS} -- run build_contacts.py first")
        return 1

    print(f"reading {CONTACTS.name} ...")
    advisors = load()
    resolved, ambiguous, report = build(advisors)

    # byCrd is the direction the SERVER needs.
    #
    # Without it a follow-up had to take its recipient from the activity log --
    # which is empty until the sweep has observed somebody, so on day one a
    # follow-up could reach nobody at all. Circular, and the error a rep saw
    # ("no email address has been observed for this advisor") described the
    # symptom rather than the cause.
    #
    # The alternative was to let the client name the address and verify it
    # against byEmail. This is better: the server looks it up itself, so there
    # is no client input to check and no trust question to reason about. It
    # costs about a megabyte on a blob that is read once per cold start.
    data = {"byEmail": resolved, "ambiguous": ambiguous,
            "byDomain": report["by_domain"], "byCrd": report["by_crd"],
            "internalCrds": report["internal"]}

    # `generated` moves on every run, so the FILE can never be a stable identity
    # for the universe it describes. contentHash covers the data and nothing
    # else, which is what "did the advisor universe actually change?" means --
    # and it is the right thing for an upload step or a staleness check to
    # compare, rather than a timestamp that always differs or a file hash that
    # differs for the wrong reason.
    body = json.dumps(data, separators=(",", ":"), sort_keys=True)
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    payload = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": "contacts.json",
        "contentHash": content_hash,
        "counts": {"advisors": report["rows"], "withEmail": report["with_email"],
                   "resolved": len(resolved), "ambiguous": len(ambiguous),
                   "domains": len(report["by_domain"]),
                   "addressable": len(report["by_crd"])},
        **data,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    # mtime=0 keeps the gzip HEADER out of the diff; the payload's `generated`
    # field still moves, so compare contentHash, never the file's own hash.
    with gzip.GzipFile(filename="", mode="wb", fileobj=OUT.open("wb"), mtime=0) as fh:
        fh.write(raw)

    print()
    print(f"  advisors          {report['rows']:,}")
    print(f"  with an address   {report['with_email']:,}")
    print(f"  resolved 1:1      {len(resolved):,}")
    print(f"  ambiguous         {len(ambiguous):,}  (published, never guessed)")
    print(f"  domains           {len(report['by_domain']):,}")
    print(f"  addressable CRDs  {len(report['by_crd']):,}  (can receive a follow-up)")
    print(f"  internal          {len(report['internal'])}  (our own people -- excluded, never tracked)")
    print(f"  uncompressed      {len(raw) / 1e6:.1f} MB")
    print(f"  written           {OUT.stat().st_size / 1e6:.2f} MB  {OUT}")

    if report["repaired"]:
        print(f"\n  REPAIRED {len(report['repaired'])} -- fix these upstream:")
        for crd, before, after, why in report["repaired"][:10]:
            print(f"      {crd}  {why:<12} {before!r} -> {after}")
    if report["rejected"]:
        print(f"\n  REJECTED {len(report['rejected'])} -- not a usable address:")
        for crd, raw_value in report["rejected"][:10]:
            print(f"      {crd}  {raw_value!r}")

    if args.upload:
        return upload(OUT)
    print("\nnot uploaded. --upload needs AZURE_STORAGE_CONNECTION_STRING.")
    return 0


def upload(path: pathlib.Path) -> int:
    """Imported lazily so building the artefact never needs the Azure SDK."""
    import os
    try:
        from azure.storage.blob import BlobServiceClient, ContentSettings
    except ImportError:
        print("\nazure-storage-blob is not installed:  pip install azure-storage-blob")
        return 1
    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
    if not conn:
        print("\nAZURE_STORAGE_CONNECTION_STRING is not set.")
        return 1
    service = BlobServiceClient.from_connection_string(conn)
    container = service.get_container_client(BLOB_CONTAINER)
    try:
        container.create_container()
    except Exception:
        pass
    blob = container.get_blob_client(BLOB_NAME)
    with path.open("rb") as fh:
        blob.upload_blob(fh, overwrite=True, content_settings=ContentSettings(
            content_type="application/json", content_encoding="gzip"))
    print(f"\nuploaded {BLOB_CONTAINER}/{BLOB_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
