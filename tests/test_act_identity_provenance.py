from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from build_identity_ledger import select_act_pull
from identity_schema import content_hash
from import_act_identity_decisions import load_existing_decisions


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_pair(root: pathlib.Path, stamp: str) -> pathlib.Path:
    contacts = root / f"act_contacts_{stamp}.json"
    owners = root / f"act_eic_contact_{stamp}.json"
    contacts.write_text(json.dumps([{"id": "a1"}]), encoding="utf-8")
    owners.write_text(json.dumps({
        "schema_version": 1, "pull_id": stamp,
        "contacts_file": contacts.name, "contacts_sha256": sha256(contacts),
        "contacts_rows": 1, "complete": True, "failed_codes": [],
        "expected_codes": ["AA"], "queried_codes": ["AA"],
        "counts": {"AA": 1}, "conflicts": [],
        "owner_by_contact_id": {"a1": "AA"},
    }), encoding="utf-8")
    inputs = []
    for role, path in (("act_contacts", contacts),
                       ("act_eic_contact", owners)):
        inputs.append({
            "role": role, "file": path.name, "sha256": sha256(path),
            "bytes": path.stat().st_size, "rows": 1, "complete": True,
        })
    manifest = root / f"act_pull_manifest_{stamp}.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "inputs": inputs,
        "policies": {"pull_id": stamp, "pair_complete": True,
                     "partial_refreshes": "forbidden"},
    }), encoding="utf-8")
    return contacts


class ActPullProvenanceTests(unittest.TestCase):
    def test_selects_newest_complete_manifest_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            write_pair(root, "2026-08-25T000000Z")
            newest = write_pair(root, "2026-08-26T000000Z")
            selected, owner, manifest, payload = select_act_pull(root)
            self.assertEqual(newest, selected)
            self.assertEqual("act_eic_contact_2026-08-26T000000Z.json",
                             owner.name)
            self.assertEqual("act_pull_manifest_2026-08-26T000000Z.json",
                             manifest.name)
            self.assertTrue(payload["policies"]["pair_complete"])

    def test_refuses_orphan_newer_contacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            write_pair(root, "2026-08-25T000000Z")
            (root / "act_contacts_2026-08-26T000000Z.json").write_text(
                "[]", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "orphan/newer"):
                select_act_pull(root)

    def test_refuses_manifest_artifact_hash_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            contacts = write_pair(root, "2026-08-26T000000Z")
            contacts.write_text('[{"id":"changed"}]', encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "byte count changed|hash changed"):
                select_act_pull(root)


class DecisionStoreProvenanceTests(unittest.TestCase):
    def test_refuses_corrupted_existing_decisions_before_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "act_identity_decisions.json"
            core = {"schemaVersion": 1, "decisions": {
                "a1": {"link_decision": "approve"}}}
            path.write_text(json.dumps({
                **core, "contentHash": content_hash(core)[:-1] + "0",
            }), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "contentHash is invalid"):
                load_existing_decisions(path)

    def test_accepts_valid_existing_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "act_identity_decisions.json"
            core = {"schemaVersion": 1, "decisions": {
                "bo": {"link_decision": "approve"}}}
            path.write_text(json.dumps({
                **core, "contentHash": content_hash(core),
            }), encoding="utf-8")
            self.assertEqual(core["decisions"], load_existing_decisions(path))


if __name__ == "__main__":
    unittest.main()
