"""Approved identity and exact-email ACT activity routes for the API.

The contacts mapping remains approved-identity-only. activity_contacts is a
separate exact-email route for mirroring a call against the selected address or
a confirmed Outlook send. It never approves a CRD, name, asset, or recipient.
The legacy fuzzy crosswalk is not an input.
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
from identity_normalize import is_generic_email, normalize_email
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


def activity_pairs(manifest: dict, approved: dict[str, str]) -> dict[str, dict[str, str]]:
    '''Unique recipient-email routes for ACT activity, not CRD identity authority.'''
    fact = manifest.get('actSource') or {}
    source = ROOT / 'data' / 'raw' / pathlib.Path(str(fact.get('file', ''))).name
    if not source.is_file() or sha256_file(source) != fact.get('sha256'):
        raise SystemExit('ACT source does not match identity manifest')
    rows = json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(rows, list) or len(rows) != int(fact.get('rows') or -1):
        raise SystemExit('ACT source row count does not match identity manifest')
    contacts = json.loads((ROOT / 'webapp' / 'data' / 'contacts.json').read_text(
        encoding='utf-8')).get('advisors') or {}

    def personal(value: object) -> str:
        email = normalize_email(value)
        return email if email and not is_generic_email(email) else ''

    act_by_email: dict[str, list[str]] = {}
    for row in rows:
        email = personal(row.get('emailAddress'))
        act_id = str(row.get('id') or '').strip()
        if email and act_id:
            act_by_email.setdefault(email, []).append(act_id)
    map_by_email: dict[str, list[str]] = {}
    for crd, row in contacts.items():
        email = personal((row or {}).get('e'))
        if email:
            map_by_email.setdefault(email, []).append(str(crd))

    result = {}
    for email, crds in map_by_email.items():
        ids = act_by_email.get(email, [])
        if len(crds) != 1 or len(ids) != 1:
            continue
        crd, act_id = crds[0], ids[0]
        if approved.get(crd, act_id) != act_id:
            continue
        result[crd] = {'id': act_id, 'email': email}
    return dict(sorted(result.items()))


def main() -> None:
    mapping, manifest = approved_pairs()
    activity = activity_pairs(manifest, mapping)
    payload = {
        "note": ("contacts holds CRD-approved identity routes. activity_contacts "
                 "holds separate one-to-one exact-email routes for mirroring "
                 "actual app activity only; it grants no SEC identity authority."),
        "built_utc": manifest.get("generatedUtc", ""),
        "identity_manifest_hash": manifest.get("contentHash", ""),
        "act_source": (manifest.get("actSource") or {}).get("file", ""),
        "act_source_sha256": (manifest.get("actSource") or {}).get("sha256", ""),
        "contacts": mapping,
    }
    payload['activity_contacts'] = activity
    payload['contact_source_sha256'] = sha256_file(
        ROOT / 'webapp' / 'data' / 'contacts.json')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True),
                   encoding="utf-8")
    print(f'[*] wrote {OUT}: {len(mapping):,} approved Act routes')


if __name__ == "__main__":
    main()
