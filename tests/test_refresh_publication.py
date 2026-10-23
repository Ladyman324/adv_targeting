"""Post-deployment receipt fixtures: no network, deployment, or Azure writes."""
import datetime as dt
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import refresh_core as core
import refresh_publication as publication
import refresh_release as release
import test_refresh_release as release_fixture


class PublicationReceiptTests(unittest.TestCase):
    def setUp(self):
        # Reuse the disposable verifier fixture, not its test class namespace.
        self.fixture = release_fixture.ReleaseVerificationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.descriptor = {"schemaVersion": 1, "candidate": {"id": "fixture",
            "createdUtc": "2026-10-22T12:00:00Z", "codeCommit": "a" * 40,
            "staticDataFingerprint": "20261022T120000Z-123456", "recipientRegistryHash": "b" * 64},
            "sources": [{"id": "act", "label": "ACT!", "sha256": "c" * 64},
                        {"id": "roster:ubs", "label": "UBS", "sha256": None}], "publication": None}
        self.descriptor_file = f.stage / "api/shared/data-freshness.json"
        core.atomic_json(self.descriptor_file, self.descriptor)
        f.manifest["validation"]["artifacts"] = core.facts(core.paths_matching(f.stage, release.ARTIFACT_PATTERNS), f.stage)
        f.manifest["validation"]["artifactHash"] = core.digest(f.manifest["validation"]["artifacts"])
        f.candidate["validationHash"] = core.digest(f.manifest["validation"])
        f.seal()
        self.options = {"published_utc": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat(),
            "api_commit": "a" * 40, "static_fingerprint": "20261022T120000Z-123456",
            "recipient_registry_hash": "b" * 64, "confirm_runtime_verified": True}

    def run_receipt(self, **overrides):
        return publication.prepare_receipt(self.fixture.root, "fixture", **{**self.options, **overrides})

    def test_valid_receipt_is_separate_and_candidate_stays_verified_unchanged(self):
        f = self.fixture
        candidate_before = core.facts([p for p in f.destination.rglob("*") if p.is_file()], f.destination)
        descriptor_before = self.descriptor_file.read_bytes()
        manifest_before = (f.directory / "run.json").read_bytes()
        result = self.run_receipt()
        receipt_path = Path(result["receiptFile"])
        self.assertEqual(receipt_path.resolve(), (f.directory / "publication/receipt.json").resolve())
        self.assertEqual(result["settingName"], "DATA_FRESHNESS_PUBLICATION_RECEIPT_JSON")
        self.assertFalse(result["settingChanged"])
        self.assertFalse(result["publishAuthorized"])
        receipt = core.read_json(receipt_path)
        self.assertTrue(receipt["verified"])
        self.assertEqual(receipt["sourceHashes"], {"act": "c" * 64})
        self.assertEqual(receipt["apiCommit"], "a" * 40)
        self.assertEqual(self.descriptor_file.read_bytes(), descriptor_before)
        self.assertEqual((f.directory / "run.json").read_bytes(), manifest_before)
        self.assertEqual(core.facts([p for p in f.destination.rglob("*") if p.is_file()], f.destination), candidate_before)
        self.assertTrue(release.verify_release(f.root, "fixture", production=True)["verified"])

    def test_explicit_runtime_confirmation_required(self):
        with self.assertRaisesRegex(core.RefreshError, "confirmation"):
            self.run_receipt(confirm_runtime_verified=False)
        self.assertFalse((self.fixture.directory / "publication").exists())

    def test_observed_runtime_ids_must_match_exactly(self):
        for changes in [{"api_commit": "d" * 40}, {"api_commit": "a" * 7},
                        {"static_fingerprint": "other-static-data"}, {"recipient_registry_hash": "d" * 64}]:
            with self.subTest(changes=changes):
                with self.assertRaises(core.RefreshError):
                    self.run_receipt(**changes)
        self.assertFalse((self.fixture.directory / "publication").exists())

    def test_publication_time_must_be_valid_timezone_aware_and_not_future(self):
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        for value in ["bad", "2026-10-22", "2026-10-22T12:00:00", "2026-02-31T12:00:00Z", future]:
            with self.subTest(value=value):
                with self.assertRaises(core.RefreshError):
                    self.run_receipt(published_utc=value)
        self.assertFalse((self.fixture.directory / "publication").exists())

    def test_dirty_source_cannot_be_attested_for_production(self):
        f = self.fixture
        f.candidate["dirty"] = True; f.manifest["codeDirty"] = True; f.seal()
        with self.assertRaisesRegex(core.RefreshError, "Production requires"):
            self.run_receipt()
        self.assertFalse((f.directory / "publication").exists())

    def test_skipped_regressions_cannot_be_attested(self):
        f = self.fixture
        f.manifest["validation"]["runTests"] = False
        f.candidate["validationHash"] = core.digest(f.manifest["validation"]); f.seal()
        with self.assertRaisesRegex(core.RefreshError, "Production requires"):
            self.run_receipt()

    def test_changed_package_cannot_be_attested(self):
        self.fixture.write_package("api.tgz", b"changed-package")
        with self.assertRaises(core.RefreshError):
            self.run_receipt()


if __name__ == "__main__":
    unittest.main()
