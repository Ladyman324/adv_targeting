"""Exact-snapshot operator review; all controller writes are disposable."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import refresh_core as core
import refresh_acquire as acquire
import refresh_data as cli
import refresh_review as review
import refresh_resilience as resilience
from test_refresh_acquire import pair
from test_refresh_resilience import records, write


class AcquisitionReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"; self.root.mkdir()
        self.stage = Path(self.temp.name) / "stage"; self.stage.mkdir()
        pair(self.stage / "data/raw")
        self.baseline = write(self.stage / "data/raw/firm_rosters/edward_jones_2026-09-22.csv", records())
        self.original = self.baseline.read_bytes()
        self.source = write(Path(self.temp.name) / "incoming/edward_jones_2026-10-23.csv", records()[:70])
        self.directory = core.run_path(self.root, "pilot"); self.directory.mkdir(parents=True)
        core.atomic_json(self.stage / ".refresh-workspace.json", {"runId": "pilot", "ownerToken": "token"})
        self.manifest = {"schemaVersion": 1, "id": "pilot", "ownerToken": "token",
                         "workspaceRoot": str(self.stage), "baselineRoot": str(self.root),
                         "mode": "selected-firm", "firms": ["edward_jones"], "codeFiles": {},
                         "codeNormalization": {}, "inputs": {}, "imports": [], "attempts": [],
                         "steps": {}, "warnings": [], "status": "ready"}
        self.pin()

    def pin(self):
        self.manifest["inputs"] = core.facts(core.source_paths(self.stage), self.stage)
        core.save_run(self.directory, self.manifest)

    def hold(self):
        with self.assertRaisesRegex(core.RefreshError, "held candidate"):
            acquire.import_snapshot(self.root, "pilot", "roster:edward_jones", self.source)
        files = list((self.stage / "data/refresh/acquisition_reviews").rglob("comparison.json"))
        self.assertEqual(len(files), 1)
        self.folder = files[0].parent
        self.report = core.read_json(files[0])
        self.identity = resilience.comparison_id(self.report)
        self.codes = sorted({item["code"] for item in self.report["issues"]})

    def decide(self, decision="approve", codes=None):
        return review.review(self.root, "pilot", self.identity, decision, "Fixture operator",
                             "Verified the supplier's documented change", self.codes if codes is None else codes)

    def test_approve_is_only_attestation_then_explicit_import_selects(self):
        self.hold()
        before = core.read_json(self.directory / "run.json")
        result = self.decide()
        self.assertFalse(result["selectedDataChanged"])
        self.assertFalse(result["publicationAuthorized"])
        after = core.read_json(self.directory / "run.json")
        self.assertEqual(before["status"], after["status"])
        self.assertEqual(before["attempts"], after["attempts"])
        self.assertEqual(self.baseline.read_bytes(), self.original)
        value = acquire.import_snapshot(self.root, "pilot", "roster:edward_jones", self.source)
        self.assertEqual(value["status"], "ready")
        proof = value["imports"][-1]["historicalAcceptance"]
        self.assertEqual(proof["reviewDecisionSha256"], core.file_hash(self.folder / "decision.json"))
        self.assertEqual((self.baseline.parent / self.source.name).read_bytes(), self.source.read_bytes())
        self.assertFalse((self.root / "data/raw").exists())

    def test_reject_and_repeated_decision_never_select_data(self):
        self.hold(); self.decide("reject")
        with self.assertRaisesRegex(core.RefreshError, "immutable"):
            self.decide()
        with self.assertRaisesRegex(core.RefreshError, "held candidate"):
            acquire.import_snapshot(self.root, "pilot", "roster:edward_jones", self.source)
        self.assertEqual(self.baseline.read_bytes(), self.original)

    def test_every_issue_must_be_explicitly_acknowledged(self):
        self.hold()
        for codes in ([], self.codes + ["NOT_AN_ISSUE"]):
            with self.assertRaisesRegex(core.RefreshError, "every flagged issue"):
                self.decide(codes=codes)
        self.assertFalse((self.folder / "decision.json").exists())

    def test_malformed_input_can_be_rejected_but_not_approved(self):
        self.source.write_text('fid,faName\n1,"truncated', encoding="utf-8")
        self.hold()
        with self.assertRaisesRegex(core.RefreshError, "cannot be approved"):
            self.decide()
        self.decide("reject")
        self.assertEqual(self.baseline.read_bytes(), self.original)

    def test_unfinished_profiles_cannot_be_approved(self):
        rows = records(); rows[0]["email_status"] = "request_error"
        write(self.source, rows)
        self.hold()
        with self.assertRaisesRegex(core.RefreshError, "cannot be approved"):
            self.decide()

    def test_bootstrap_requires_explicit_review_and_then_allows_first_import(self):
        self.baseline.unlink(); self.pin()
        write(self.source, records())
        self.hold()
        self.assertEqual(self.codes, ["BASELINE_REVIEW_REQUIRED"])
        self.decide()
        result = acquire.import_snapshot(self.root, "pilot", "roster:edward_jones", self.source)
        self.assertEqual(result["imports"][-1]["records"], 100)
        self.assertIsNone(result["imports"][-1]["historicalAcceptance"]["baselineSha256"])

    def test_tampered_held_bytes_are_not_reviewable(self):
        self.hold()
        with (self.folder / "held.csv").open("a") as handle: handle.write("\n")
        with self.assertRaisesRegex(core.RefreshError, "changed"):
            self.decide()

    def test_tampered_report_cannot_be_approved(self):
        self.hold()
        report = dict(self.report); report["issues"] = []
        core.atomic_json(self.folder / "comparison.json", report)
        with self.assertRaisesRegex(core.RefreshError, "changed"):
            self.decide()

    def test_changed_baseline_invalidates_review_even_if_inputs_were_reselected(self):
        self.hold()
        write(self.baseline, records(101)); self.pin()
        with self.assertRaisesRegex(core.RefreshError, "changed"):
            self.decide()

    def test_tampered_decision_does_not_authorize_next_import(self):
        self.hold(); self.decide()
        file = self.folder / "decision.json"
        decision = core.read_json(file); decision["candidateSha256"] = "a" * 64
        core.atomic_json(file, decision)
        with self.assertRaisesRegex(core.RefreshError, "no longer binds"):
            acquire.import_snapshot(self.root, "pilot", "roster:edward_jones", self.source)
        self.assertEqual(self.baseline.read_bytes(), self.original)

    def test_changed_review_attribution_cannot_bypass_run_audit_binding(self):
        self.hold(); self.decide()
        file = self.folder / "decision.json"
        decision = core.read_json(file); decision["reviewer"] = "Someone different"
        core.atomic_json(file, decision)
        with self.assertRaisesRegex(core.RefreshError, "run audit trail"):
            acquire.import_snapshot(self.root, "pilot", "roster:edward_jones", self.source)
        self.assertEqual(self.baseline.read_bytes(), self.original)

    def test_wrong_mode_and_unknown_comparison_refused(self):
        self.hold()
        with self.assertRaises(core.RefreshError):
            review.review(self.root, "pilot", "../escape", "approve", "Operator", "A sufficiently long reason", self.codes)
        self.manifest["mode"] = "act-only"; self.pin()
        with self.assertRaisesRegex(core.RefreshError, "outside"):
            self.decide()

    def test_cli_dry_run_does_not_record_decision(self):
        self.hold()
        before = (self.directory / "run.json").read_bytes()
        args = ["--root", str(self.root), "review-acquisition", "--run", "pilot", "--comparison", self.identity,
                "--decision", "approve", "--reviewer", "Operator", "--reason", "A sufficiently long review reason", "--dry-run"]
        with patch("builtins.print"):
            self.assertEqual(cli.main(args), 0)
        self.assertEqual((self.directory / "run.json").read_bytes(), before)
        self.assertFalse((self.folder / "decision.json").exists())

    def test_cli_proposal_from_held_pull_does_not_heal_failure_or_select_data(self):
        import refresh_repair as repair
        import refresh_repair_runner as runner
        from test_refresh_repair import SOURCE
        from test_refresh_repair_runner import response
        adapter = self.stage / repair.ADAPTER
        adapter.parent.mkdir(parents=True); adapter.write_text(SOURCE, encoding="utf-8")
        controller = self.root / repair.ADAPTER
        controller.parent.mkdir(parents=True); controller.write_bytes(adapter.read_bytes())
        self.manifest["codeFiles"] = core.facts([adapter], self.stage)
        self.manifest["codeNormalization"] = {repair.ADAPTER: core.normalized_code_hash(adapter, repair.ADAPTER)}
        self.pin()
        rows = records()
        for row in rows[:30]: row["email"] = ""
        write(self.source, rows)
        self.hold()
        files = list((self.stage / "data/refresh/repair_requests").glob("*/request.json"))
        self.assertEqual(len(files), 1)
        request = core.read_json(files[0])
        fixture = Path(self.temp.name) / "fixture.json"
        core.atomic_json(fixture, {"kind": "synthetic", "cases": [
            {"html": "<html>Synthetic parser change</html>", "expectedEmails": [], "expectedBio": None}]})
        before = core.read_json(self.directory / "run.json")
        args = ["--root", str(self.root), "propose-repair", "--run", "pilot", "--request", request["id"],
                "--fixture", str(fixture), "--model", "operator-selected-model", "--operator", "Fixture operator",
                "--reason", "Approved synthetic fixture for the held parser failure", "--allow-ai",
                "--confirm-public-or-synthetic-fixture"]
        with patch.object(runner, "request_model", return_value=response()) as provider, patch("builtins.print"):
            self.assertEqual(cli.main(args), 0)
            self.assertEqual(provider.call_count, 1)
        after = core.read_json(self.directory / "run.json")
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["attempts"], before["attempts"])
        self.assertEqual(after["imports"], [])
        self.assertEqual(self.baseline.read_bytes(), self.original)
        self.assertEqual(adapter.read_text(encoding="utf-8"), SOURCE)
        self.assertEqual(after["repairRequests"][request["id"]]["status"], "proposal_awaiting_review")
        self.assertFalse((self.root / "data/raw").exists())


if __name__ == "__main__":
    unittest.main()
