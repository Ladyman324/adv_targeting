"""Verifier gate contracts using disposable, synthetic prepared candidates.

These tests exercise verify_release itself, not a mock verifier. Archive bytes
are synthetic: actual API/archive shape checks remain in build_api.sh tests.
"""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import refresh_core as core
import refresh_release as release


def private_fixture(stage):
    """Minimal complete private dependencies; bytes are synthetic, never live."""
    for relative in release.REQUIRED_PRIVATE:
        path = stage / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(b"fixture")
    blob = "approved_recipients/releases/" + "a" * 64 + "/01.json.gz"
    core.atomic_json(stage / "api/shared/approved-recipient-release.json", {"registryContentHash": "a" * 64})
    core.atomic_json(stage / "data/identity/approved_recipients_manifest.json", {"shards": {"one": {"blob": blob}}})
    shard = stage / "data/identity/approved_recipients_shards/01.json.gz"
    shard.parent.mkdir(parents=True, exist_ok=True)
    shard.write_bytes(b"synthetic-private-recipient-shard")
    return {"lookups/" + blob: shard,
            "lookups/approved_recipients/releases/" + "a" * 64 + "/manifest.json": stage / "data/identity/approved_recipients_manifest.json",
            "lookups/advisor_emails.json.gz": stage / "data/output/advisor_emails.json.gz"}


class ReleaseVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"; self.root.mkdir()
        self.stage = Path(self.temp.name) / "stage"; self.stage.mkdir()
        self.directory = core.run_path(self.root, "fixture"); self.directory.mkdir(parents=True)
        core.atomic_json(self.stage / ".refresh-workspace.json", {"runId": "fixture", "ownerToken": "owner"})
        core.atomic_json(self.stage / "webapp/data/pins_GA.json", {"pins": [{"id": "1", "lat": 33, "lon": -84}]})
        core.atomic_json(self.stage / "api/shared/act_contacts.json", {"contacts": {"1": "act-fixture"}})
        self.destination = self.directory / "candidate"; self.destination.mkdir()
        self.write_package("api.tgz", b"synthetic-api-archive")
        self.write_package("static.tgz", b"synthetic-static-archive")
        self.shard = "lookups/approved_recipients/releases/" + "a" * 64 + "/01.json.gz"
        for relative, source in private_fixture(self.stage).items():
            self.write_package(relative, source.read_bytes())
        artifacts = core.facts(core.paths_matching(self.stage, release.ARTIFACT_PATTERNS), self.stage)
        self.manifest = {"schemaVersion": 1, "id": "fixture", "ownerToken": "owner",
            "workspaceRoot": str(self.stage), "baselineRoot": str(self.root), "mode": "full",
            "status": "candidate_ready", "codeFiles": {}, "codeNormalization": {}, "codeHash": core.digest({}),
            "inputs": core.facts(core.source_paths(self.stage, "full"), self.stage), "codeDirty": False,
            "validation": {"status": "success", "runTests": True, "artifacts": artifacts, "artifactHash": core.digest(artifacts)}}
        self.candidate = {"schemaVersion": 1, "runId": "fixture", "createdUtc": "2026-10-23T12:00:00Z",
            "codeCommit": "a" * 40, "codeHash": self.manifest["codeHash"], "dirty": False,
            "publishAuthorized": False, "validationHash": core.digest(self.manifest["validation"]),
            "sources": [], "files": core.facts([p for p in self.destination.rglob("*") if p.is_file()], self.destination)}
        self.seal()

    def write_package(self, relative, content):
        path = self.destination / relative
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
        return path

    def seal(self, bind=True):
        content_hash = core.digest(self.candidate)
        core.atomic_json(self.destination / "release.json", {**self.candidate, "contentHash": content_hash})
        if bind:
            self.manifest["candidate"] = {"manifest": "candidate/release.json", "contentHash": content_hash}
        core.save_run(self.directory, self.manifest)

    def test_valid_bound_candidate_verifies_but_never_authorizes_publish(self):
        for production in (False, True):
            result = release.verify_release(self.root, "fixture", production=production)
            self.assertTrue(result["verified"])
            self.assertFalse(result["publishAuthorized"])
            self.assertEqual(result["files"], 5)

    def test_changed_validated_artifact_is_rejected(self):
        (self.stage / "webapp/data/pins_GA.json").write_text('{"different":true}')
        with self.assertRaisesRegex(core.RefreshError, "artifacts changed"):
            release.verify_release(self.root, "fixture")

    def test_added_validated_artifact_is_rejected(self):
        core.atomic_json(self.stage / "webapp/data/unvalidated.json", {"extra": True})
        with self.assertRaisesRegex(core.RefreshError, "artifacts changed"):
            release.verify_release(self.root, "fixture")

    def test_archive_changed_after_preparation_is_rejected(self):
        self.write_package("api.tgz", b"changed")
        with self.assertRaisesRegex(core.RefreshError, "package or dependency changed"):
            release.verify_release(self.root, "fixture")

    def test_private_shard_changed_after_preparation_is_rejected(self):
        self.write_package(self.shard, b"changed-shard")
        with self.assertRaisesRegex(core.RefreshError, "package or dependency changed"):
            release.verify_release(self.root, "fixture")

    def test_private_shard_missing_after_preparation_is_rejected(self):
        (self.destination / self.shard).unlink()
        with self.assertRaises(core.RefreshError):
            release.verify_release(self.root, "fixture")

    def test_dirty_candidate_is_local_only(self):
        self.candidate["dirty"] = True; self.manifest["codeDirty"] = True; self.seal()
        self.assertTrue(release.verify_release(self.root, "fixture")["verified"])
        with self.assertRaisesRegex(core.RefreshError, "Production requires"):
            release.verify_release(self.root, "fixture", production=True)

    def test_skipped_regressions_are_local_only(self):
        self.manifest["validation"]["runTests"] = False
        self.candidate["validationHash"] = core.digest(self.manifest["validation"]); self.seal()
        self.assertTrue(release.verify_release(self.root, "fixture")["verified"])
        with self.assertRaisesRegex(core.RefreshError, "Production requires"):
            release.verify_release(self.root, "fixture", production=True)

    def test_candidate_cannot_reseal_modified_packages_without_pinned_run_binding(self):
        path = self.write_package("api.tgz", b"replacement-with-resealed-manifest")
        self.candidate["files"]["api.tgz"] = core.facts([path], self.destination)["api.tgz"]
        self.seal(bind=False)
        with self.assertRaises(core.RefreshError):
            release.verify_release(self.root, "fixture")

    def test_unlisted_extra_candidate_dependency_is_rejected(self):
        self.write_package("lookups/unlisted.json.gz", b"unexpected")
        with self.assertRaises(core.RefreshError):
            release.verify_release(self.root, "fixture")

    def test_declared_candidate_path_cannot_escape_package_root(self):
        outside = self.directory / "outside.bin"; outside.write_bytes(b"outside")
        self.candidate["files"]["../outside.bin"] = {"sha256": core.file_hash(outside), "bytes": outside.stat().st_size}
        self.seal()
        with self.assertRaises(core.RefreshError):
            release.verify_release(self.root, "fixture")

    def test_missing_private_stage_dependencies_after_validation_are_rejected(self):
        for relative in list(release.REQUIRED_PRIVATE) + ["data/identity/approved_recipients_shards/01.json.gz"]:
            path = self.stage / relative
            saved = path.read_bytes()
            path.unlink()
            try:
                with self.subTest(relative=relative), self.assertRaises(core.RefreshError):
                    release.verify_release(self.root, "fixture")
            finally:
                path.write_bytes(saved)

    def test_candidate_cannot_omit_activity_lookup_or_private_registry(self):
        for relative in ("lookups/advisor_emails.json.gz",
                         "lookups/approved_recipients/releases/" + "a" * 64 + "/manifest.json", self.shard):
            info = self.candidate["files"].pop(relative)
            self.seal()
            with self.subTest(relative=relative), self.assertRaisesRegex(core.RefreshError, "Required candidate"):
                release.verify_release(self.root, "fixture")
            self.candidate["files"][relative] = info
            self.seal()

    def test_declared_absolute_candidate_path_is_rejected(self):
        outside = self.directory / "outside.bin"; outside.write_bytes(b"outside")
        self.candidate["files"][str(outside.resolve())] = {"sha256": core.file_hash(outside), "bytes": outside.stat().st_size}
        self.seal()
        with self.assertRaises(core.RefreshError):
            release.verify_release(self.root, "fixture")


if __name__ == "__main__":
    unittest.main()
