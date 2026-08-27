"""Approved identity ledger -> API CRD-to-Act contact lookup.

This artifact is used for Act history reads and writes.  It deliberately has
one authority: unique rows that the identity ledger marks approved and
syncable.  The legacy fuzzy crosswalk is not an input.
"""
from __future__ import annotations

import json
import pathlib
import sys
from collections import Counter

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from contact_provenance import sha256_file
from identity_schema import (IDENTITY_DIRNAME, LINKS_FILENAME,
                             MANIFEST_FILENAME, content_hash)

IDENTITY = ROOT / "data" / IDENTITY_DIRNAME
OUT = ROOT / "api" / "shared" / "act_contacts.json"


def approved_pairs() -> tuple[dict[str, str], dict]:
    manifest_path = IDENTITY / MANIFEST_FILENAME
    links_path = IDENTITY / LINKS_FILENAME
    if not manifest_path.exists() or not links_path.exists():
        raise SystemExit("Identity ledger is missing; run src/build_identity_ledger.py")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    core = {k: v for k, v in manifest.items()
            if k not in {"generatedUtc", "contentHash"}}
    if manifest.get("contentHash") != content_hash(core):
        raise SystemExit("Identity manifest contentHash is invalid")
    link_meta = (manifest.get("outputs") or {}).get(LINKS_FILENAME) or {}
    if link_meta.get("sha256") != sha256_file(links_path):
        raise SystemExit("Identity links do not match the manifest")

    links = pd.read_parquet(links_path).fillna("")
    if len(links) != int(link_meta.get("rows") or -1):
        raise SystemExit("Identity links row count does not match the manifest")
    approved = links[(links["identity_status"] == "approved")
                     & links["can_sync_act"].astype(bool)].copy()
    approved["advisor_crd"] = approved["advisor_crd"].astype(str).str.strip()
    approved["source_record_id"] = approved["source_record_id"].astype(str).str.strip()
    approved = approved[
        approved["advisor_crd"].str.fullmatch(r"\d{3,12}", na=False)
        & approved["source_record_id"].ne("")]

    crd_counts = Counter(approved["advisor_crd"])
    guid_counts = Counter(approved["source_record_id"])
    safe = approved[
        approved["advisor_crd"].map(crd_counts).eq(1)
        & approved["source_record_id"].map(guid_counts).eq(1)]
    mapping = dict(sorted(zip(safe["advisor_crd"], safe["source_record_id"])))
    return mapping, manifest


def main() -> None:
    mapping, manifest = approved_pairs()
    payload = {
        "note": ("CRD -> Act contact id. Unique, approved identity-ledger links "
                 "only; an absent CRD never reads or writes an Act contact."),
        "built_utc": manifest.get("generatedUtc", ""),
        "identity_manifest_hash": manifest.get("contentHash", ""),
        "act_source": (manifest.get("actSource") or {}).get("file", ""),
        "act_source_sha256": (manifest.get("actSource") or {}).get("sha256", ""),
        "contacts": mapping,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True),
                   encoding="utf-8")
    print(f"[*] wrote {OUT}: {len(mapping):,} approved Act routes")


if __name__ == "__main__":
    main()
