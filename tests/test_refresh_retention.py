import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import refresh_core as core
import refresh_retention as retention


@unittest.skipUnless(os.name == "nt", "The scheduled local workspace policy is Windows-only")
class LocalRetentionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.repo = self.base / "repo"
        self.parent = self.base / "EIC/AdvisorRefresh/workspaces"
        self.parent.mkdir(parents=True)
        self.publishing = self.repo / "data/refresh/publishing"
        self.publishing.mkdir(parents=True)
        env = patch.dict(os.environ, {"LOCALAPPDATA": str(self.base)})
        env.start()
        self.addCleanup(env.stop)
        proof = patch("refresh_publish_azure.archive_bundle")
        self.archive_bundle = proof.start()
        self.addCleanup(proof.stop)
        git = patch("refresh_retention.subprocess.run")
        git.start().return_value.returncode = 0
        self.addCleanup(git.stop)

    def workspace(self, run_id, day, *, source=b"retained bytes", published=False, local=True, status=None):
        stage = (self.parent if local else self.base / "share") / run_id
        stage.mkdir(parents=True)
        directory = core.run_path(self.repo, run_id)
        (directory / "publication").mkdir(parents=True)
        token = "owned-" + run_id
        created = f"2026-11-{day:02}T09:00:00+00:00"
        data = stage / "input.json"
        data.write_bytes(source)
        core.atomic_json(stage / ".refresh-workspace.json", {"runId": run_id, "ownerToken": token})
        core.atomic_json(directory / "run.json", {
            "id": run_id, "schemaVersion": 1, "workspaceRoot": str(stage), "ownerToken": token,
            "createdUtc": created, "codeCommit": "c" * 40,
            "status": status or ("candidate_ready" if published else "validation_failed"),
            "candidate": {"contentHash": run_id},
            "inputs": core.facts([data], stage),
        })
        if published:
            core.atomic_json(directory / "publication/automatic.json", {
                "status": "completed", "candidateHash": run_id, "publishedUtc": created,
                "baseline": {"verified": True}, "backup": str(self.publishing / "backup.json"),
            })
            core.atomic_json(directory / "publication/receipt.json", {"verified": True})
        return stage

    def activate(self, current, backup):
        core.atomic_json(self.publishing / "current.json", {"run_id": current, "baseline": {"verified": True}})
        core.atomic_json(self.publishing / "backup.json", {"previous": {"root": str(self.repo), "run_id": backup}})

    def test_preview_and_cleanup_keep_two_complete_local_publications(self):
        oldest = self.workspace("oldest", 1, published=True)
        first_local = self.workspace("first-local", 2, published=True)
        second_local = self.workspace("second-local", 3, published=True)
        live = self.workspace("live", 4, published=True, local=False)
        failed = self.workspace("failed", 1)
        prepared = self.workspace("prepared", 1, status="candidate_ready")
        active = self.workspace("active", 1, status="building")
        unmatched = self.workspace("unmatched", 1, source=b"unique source")
        self.activate("live", "second-local")

        preview = retention.maintain(self.repo)
        self.assertEqual({r["runId"] for r in preview["workspaces"]}, {"oldest", "failed", "prepared"})
        self.assertTrue(oldest.is_dir())
        self.assertTrue(failed.is_dir())
        report = retention.maintain(self.repo, apply=True, expected_run_id="live")
        self.assertEqual({r["status"] for r in report["workspaces"]}, {"removed"})
        self.assertFalse(oldest.exists())
        self.assertFalse(failed.exists())
        self.assertFalse(prepared.exists())
        for kept in (first_local, second_local, live, active, unmatched):
            self.assertTrue(kept.is_dir())
        archived = core.run_path(self.repo, "oldest")
        self.assertTrue((archived / "run.json").is_file())
        self.assertTrue((archived / "publication/receipt.json").is_file())

    def test_archive_verification_failure_holds_old_workspace(self):
        oldest = self.workspace("oldest", 1, published=True)
        self.workspace("rollback", 2, published=True)
        self.workspace("live", 3, published=True)
        self.activate("live", "rollback")

        def verify(_root, run_id):
            if run_id == "oldest":
                raise core.RefreshError("Packaged archive no longer matches")

        self.archive_bundle.side_effect = verify
        report = retention.maintain(self.repo, apply=True)
        self.assertEqual(report["workspaces"][0]["status"], "held")
        self.assertTrue(oldest.is_dir())


if __name__ == "__main__":
    unittest.main()
