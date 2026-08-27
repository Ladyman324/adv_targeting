from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from act_client import ActError, census  # noqa: E402
from act_eic_contact import CODES, extract  # noqa: E402
from contact_provenance import (  # noqa: E402
    ProvenanceError,
    atomic_create_json,
    build_manifest,
    file_facts,
    load_manifest,
    validate_manifest,
    validate_owner_artifact,
    write_manifest,
)


class PagingAct:
    def __init__(self, pages):
        self.pages = list(pages)

    def get(self, _path, **_params):
        return self.pages.pop(0) if self.pages else []


class OwnerAct:
    def __init__(self, rows_by_code=None, fail_code="", metadata_codes=None):
        self.rows_by_code = rows_by_code or {}
        self.fail_code = fail_code
        self.metadata_codes = metadata_codes or sorted(CODES)

    def post(self, _path, body):
        code = body[0]["values"][0]["dataItem"]
        if code == self.fail_code:
            raise ActError("fixture failure")
        return list(self.rows_by_code.get(code, []))

    def get(self, _path):
        return [{"name": "Contacts", "fields": [{
            "name": "EIC Contact", "table": "TBL_CONTACT",
            "column": "REFERREDBY", "list": {"items": self.metadata_codes},
        }]}]


def owner_payload(contacts, contact_hash, **changes):
    payload = {
        "schema_version": 1,
        "pull_id": "2026-08-26T120000Z",
        "contacts_file": contacts.name,
        "contacts_sha256": contact_hash,
        "contacts_rows": 2,
        "complete": True,
        "failed_codes": [],
        "expected_codes": sorted(CODES),
        "queried_codes": sorted(CODES),
        "counts": {code: (1 if code == "SH" else 0) for code in CODES},
        "conflicts": [],
        "owner_by_contact_id": {"act-1": "SH"},
    }
    payload.update(changes)
    return payload


class ImmutableArtifactTests(unittest.TestCase):
    def test_create_only_never_overwrites_existing_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            target = pathlib.Path(td) / "artifact.json"
            atomic_create_json(target, {"version": 1})
            original = target.read_bytes()
            with self.assertRaisesRegex(ProvenanceError, "already exists"):
                atomic_create_json(target, {"version": 2})
            self.assertEqual(original, target.read_bytes())

    def test_incomplete_census_never_publishes_raw_file(self):
        with tempfile.TemporaryDirectory() as td:
            target = pathlib.Path(td) / "act_contacts_2026-08-26T120000Z.json"
            act = PagingAct([[{"id": "one"}], [{"id": "one"}]])
            with self.assertRaisesRegex(ActError, "incomplete"):
                census(act, "contacts", page=1, cap=3, save_to=target)
            self.assertFalse(target.exists())

    def test_complete_census_publishes_once(self):
        with tempfile.TemporaryDirectory() as td:
            target = pathlib.Path(td) / "act_contacts_2026-08-26T120000Z.json"
            with mock.patch("act_client.write_reports"):
                result = census(PagingAct([[{"id": "one"}], []]), "contacts",
                                page=2, cap=5, save_to=target)
            self.assertTrue(result.complete)
            self.assertEqual([{"id": "one"}], json.loads(target.read_text()))
            with self.assertRaisesRegex(ProvenanceError, "already exists"):
                census(PagingAct([]), "contacts", 2, 5, target)


class OwnerBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = pathlib.Path(self.temp.name)
        self.contacts = self.directory / "act_contacts_2026-08-26T120000Z.json"
        atomic_create_json(self.contacts, [{"id": "act-1"}, {"id": "act-2"}])
        self.contact_hash = file_facts(self.contacts, "act_contacts")["sha256"]
        self.owner = self.directory / "act_eic_contact_2026-08-26T120000Z.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_hash_and_id_bound_owner_artifact_is_accepted(self):
        atomic_create_json(
            self.owner, owner_payload(self.contacts, self.contact_hash))
        validated = validate_owner_artifact(self.contacts, self.owner)
        self.assertEqual("SH", validated["owner_by_contact_id"]["act-1"])

    def test_unknown_contact_id_is_rejected(self):
        atomic_create_json(self.owner, owner_payload(
            self.contacts, self.contact_hash,
            owner_by_contact_id={"not-in-pull": "SH"}))
        with self.assertRaisesRegex(ProvenanceError, "absent"):
            validate_owner_artifact(self.contacts, self.owner)

    def test_date_only_legacy_pair_is_rejected_even_when_hash_matches(self):
        contacts = self.directory / "act_contacts_2026-08-26.json"
        owner = self.directory / "act_eic_contact_2026-08-26.json"
        atomic_create_json(contacts, [{"id": "act-1"}])
        digest = file_facts(contacts, "act_contacts")["sha256"]
        payload = owner_payload(contacts, digest, pull_id="2026-08-26",
                                contacts_rows=1)
        atomic_create_json(owner, payload)
        with self.assertRaisesRegex(ProvenanceError, "date-only"):
            validate_owner_artifact(contacts, owner)

    def test_failed_owner_query_leaves_no_artifact(self):
        with self.assertRaisesRegex(ActError, "refusing to publish"):
            extract(OwnerAct(fail_code="SH"), dest=self.owner,
                    csv_dest=self.directory / "owners.csv",
                    contacts_path=self.contacts)
        self.assertFalse(self.owner.exists())

    def test_owner_query_rejects_id_not_in_bound_contacts(self):
        rows = {"SH": [{"id": "new-contact", "fullName": "New Person"}]}
        with self.assertRaisesRegex(ActError, "refusing to publish"):
            extract(OwnerAct(rows), dest=self.owner,
                    csv_dest=self.directory / "owners.csv",
                    contacts_path=self.contacts)
        self.assertFalse(self.owner.exists())

    def test_new_live_owner_code_fails_before_partial_publication(self):
        act = OwnerAct(metadata_codes=sorted(CODES) + ["NEW"])
        with self.assertRaisesRegex(ActError, "unlabelled"):
            extract(act, dest=self.owner,
                    csv_dest=self.directory / "owners.csv",
                    contacts_path=self.contacts)
        self.assertFalse(self.owner.exists())


class ManifestTests(unittest.TestCase):
    def test_build_id_is_stable_across_generation_times(self):
        with tempfile.TemporaryDirectory() as td:
            source = pathlib.Path(td) / "input.json"
            source.write_text("[]", encoding="utf-8")
            fact = file_facts(source, "fixture", rows=0)
            first = build_manifest([fact], generated_utc="2026-01-01T00:00:00Z")
            second = build_manifest([fact], generated_utc="2026-02-01T00:00:00Z")
            self.assertEqual(first["build_id"], second["build_id"])

    def test_tampered_manifest_is_rejected_on_write_and_load(self):
        with tempfile.TemporaryDirectory() as td:
            source = pathlib.Path(td) / "input.json"
            source.write_text("[]", encoding="utf-8")
            manifest = build_manifest([file_facts(source, "fixture", rows=0)])
            manifest["policies"]["unsafe"] = True
            with self.assertRaisesRegex(ProvenanceError, "build_id"):
                validate_manifest(manifest)
            target = pathlib.Path(td) / "manifest.json"
            target.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ProvenanceError, "build_id"):
                load_manifest(target)

    def test_valid_manifest_round_trips(self):
        with tempfile.TemporaryDirectory() as td:
            source = pathlib.Path(td) / "input.json"
            source.write_text("[]", encoding="utf-8")
            manifest = build_manifest([file_facts(source, "fixture", rows=0)])
            target = pathlib.Path(td) / "manifest.json"
            write_manifest(target, manifest)
            self.assertEqual(manifest["build_id"], load_manifest(target)["build_id"])


if __name__ == "__main__":
    unittest.main()
