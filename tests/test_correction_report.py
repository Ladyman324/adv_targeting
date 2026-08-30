from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from export_act_crd_corrections import (claimed_domain_conflict_mask,
                                        is_claimed_domain_conflict,
                                        report_email_domain,
                                        validate_identity_manifest)
from identity_schema import (EVIDENCE_FILENAME, LINKS_FILENAME,
                             SOURCE_RECORDS_FILENAME, content_hash)


class CorrectionReportProvenanceTests(unittest.TestCase):
    def test_report_uses_suffixed_evidence_email_domain(self):
        row = {"email_domain_ev": "ubs.com", "email_domain_act": ""}
        self.assertEqual("ubs.com", report_email_domain(row))

    def test_domain_conflict_is_a_correction_only_when_act_supplied_a_crd(self):
        rows = pd.DataFrame([
            {"claimed_crd": "", "email_domain_current_conflicts": True},
            {"claimed_crd": "123", "email_domain_current_conflicts": True},
            {"claimed_crd": "456", "email_domain_current_conflicts": False},
        ])
        self.assertEqual([False, True, False],
                         claimed_domain_conflict_mask(rows).tolist())
        self.assertFalse(is_claimed_domain_conflict(rows.iloc[0]))
        self.assertTrue(is_claimed_domain_conflict(rows.iloc[1]))

    def write_manifest(self, root: pathlib.Path) -> pathlib.Path:
        outputs = {}
        for filename in (SOURCE_RECORDS_FILENAME, EVIDENCE_FILENAME,
                         LINKS_FILENAME):
            path = root / filename
            path.write_bytes(filename.encode("utf-8"))
            outputs[filename] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "rows": 1,
            }
        core = {
            "schemaVersion": 1,
            "actSource": {"file": "act.json", "sha256": "act-sha", "rows": 1},
            "outputs": outputs,
        }
        manifest = {**core, "generatedUtc": "2026-08-27T00:00:00Z",
                    "contentHash": content_hash(core)}
        path = root / "identity_manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_valid_manifest_binds_all_report_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_manifest(root)
            self.assertEqual("act.json",
                             validate_identity_manifest(root)["actSource"]["file"])

    def test_changed_artifact_is_refused_before_report_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_manifest(root)
            (root / LINKS_FILENAME).write_bytes(b"changed")
            with self.assertRaisesRegex(SystemExit, "differs from manifest"):
                validate_identity_manifest(root)

    def test_changed_manifest_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = self.write_manifest(root)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["actSource"]["file"] = "wrong.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "content hash is invalid"):
                validate_identity_manifest(root)


if __name__ == "__main__":
    unittest.main()
