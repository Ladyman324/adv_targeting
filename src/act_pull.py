"""One command to download Act!, both halves of it, stamped the same day.

Act! will not give up its contact book in a single call. Two endpoints are
needed and they answer different questions:

    api/contacts                     the records -- 47,469 of them, 226 fields
    api/dynamic-list/contact-preview the EIC Contact owner, which api/contacts
                                     refuses to serialise at all

The second exists because Act! defines two fields whose names collide --
REFERREDBY (display name "EIC Contact") and CUST_ReferredBy_094317334 (display
name "Referred By") -- and the REST contact resource returns only the custom
one. `$select=REFERREDBY` does not reach past it either. See act_eic_contact.py.

WHY ONE COMMAND. The two files are a matched pair, and nothing about a pair of
files on disk says they were pulled together. An owner map from June sitting
beside a contact pull from August produces confident, wrong answers about who
covers whom, and nothing on screen would look unusual. Pulling both in one run
under one date stamp is what lets build_contacts.py refuse a mismatched pair
rather than quietly joining across eight weeks.

They stay SEPARATE FILES. data/raw/act_contacts_*.json is documented as a
faithful record of what api/contacts returned (docs/name_provenance.md), and
splicing another endpoint's result into those objects would make that false.
Raw files are boring and truthful; joins happen downstream.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from act_client import Act, ActError, BASE, census
from act_eic_contact import RAW, extract, stamp
from contact_provenance import (ProvenanceError, atomic_publish_file,
                                build_manifest, file_facts, json_array_facts,
                                validate_owner_artifact, write_manifest)

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--page", type=int, default=500, help="page size (default 500)")
    ap.add_argument("--cap", type=int, default=50000,
                    help="stop after this many contacts (default 50000)")
    ap.add_argument("--skip-contacts", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--contacts-file",
                    help="with --skip-contacts, exact act_contacts_*.json to reuse "
                         "(default: newest immutable pull)")
    args = ap.parse_args()

    if args.skip_contacts or args.contacts_file:
        print("[!] Partial refreshes are disabled. Contacts and owners must be "
              "observed in one immutable pull so an old contact snapshot cannot "
              "receive current owner assignments.")
        return 2

    password = os.environ.get("ACT_PASSWORD", "")
    if not password:
        print("ACT_PASSWORD is not set.\n"
              "  PowerShell:  $env:ACT_PASSWORD = 'your password in SINGLE quotes'")
        return 2

    pull_id = stamp()
    contacts_path = RAW / f"act_contacts_{pull_id}.json"
    owners_path = RAW / f"act_eic_contact_{pull_id}.json"
    manifest_path = RAW / f"act_pull_manifest_{pull_id}.json"
    RAW.mkdir(parents=True, exist_ok=True)
    for path in (contacts_path, owners_path, manifest_path):
        if path.exists():
            raise ProvenanceError(f"immutable artifact already exists: {path}")

    act = Act(args.user, password, args.db, args.base)
    act.authorize()
    print(f"authorized against {act.base} as {args.user}")
    print(f"new pull id: {pull_id}\n")

    # Neither half is visible in data/raw until both API reads and strict pair
    # validation succeed. A failed owner code therefore cannot leave a newer,
    # ownerless contact file that a downstream "newest file" glob might select.
    with tempfile.TemporaryDirectory(prefix=".act_pull_", dir=RAW) as stage:
        stage = Path(stage)
        staged_contacts = stage / contacts_path.name
        staged_owners = stage / owners_path.name
        print("=== 1/2  api/contacts ===")
        result = census(act, "contacts", args.page, args.cap, staged_contacts)
        if not result.complete or result.total <= 0:
            print(f"[!] contact pull is not complete ({result.stop_reason})")
            return 1

        print("\n=== 2/2  EIC Contact owner map ===")
        owners = extract(act, dest=staged_owners,
                         contacts_path=staged_contacts)
        try:
            validate_owner_artifact(staged_contacts, staged_owners)
        except ProvenanceError as exc:
            print(f"[!] owner artifact failed strict validation: {exc}")
            return 1

        published = []
        try:
            atomic_publish_file(staged_contacts, contacts_path)
            published.append(contacts_path)
            atomic_publish_file(staged_owners, owners_path)
            published.append(owners_path)
        except Exception:
            # Roll back only files this transaction just published. The paths
            # were checked absent above and immutable publication cannot replace
            # another run's bytes.
            for path in reversed(published):
                path.unlink(missing_ok=True)
            raise

    manifest = build_manifest(
        [json_array_facts(contacts_path, "act_contacts"),
         file_facts(owners_path, "act_eic_contact", rows=len(owners))],
        generator_files=[Path(__file__), ROOT / "src" / "act_client.py",
                         ROOT / "src" / "act_eic_contact.py",
                         ROOT / "src" / "contact_provenance.py"],
        policies={"pull_id": pull_id, "pair_complete": True,
                  "partial_refreshes": "forbidden",
                  "owner_join_key": "act_contact_id"},
    )
    try:
        write_manifest(manifest_path, manifest, create_only=True)
    except Exception:
        # The manifest is the commit marker for the pair. If it cannot be
        # published, do not leave a pair that appears committed downstream.
        owners_path.unlink(missing_ok=True)
        contacts_path.unlink(missing_ok=True)
        raise

    print("\n--- pulled ---")
    for path in (contacts_path, owners_path, manifest_path):
        size = path.stat().st_size if path.exists() else 0
        print(f"  {path.name:<34} {size:>12,} bytes")
    print(f"\n{len(owners):,} contacts carry an EIC Contact owner.")
    print("The owner artifact records the exact contact SHA-256 and row count.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ActError, ProvenanceError) as e:
        print(f"[!] {e}")
        sys.exit(1)
