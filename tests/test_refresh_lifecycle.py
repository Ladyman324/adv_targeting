"""Durable lifecycle fault injection; fixtures only, no live acquisition."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import refresh_core as core
import refresh_acquire as acquire
import refresh_release as release
import refresh_data as cli
import refresh_inventory
from test_refresh_acquire import pair, roster
from test_refresh_release import private_fixture


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.stage = Path(self.temp.name) / "stage"
        self.root.mkdir(); self.stage.mkdir()
        pair(self.stage / "data/raw")
        private_fixture(self.stage)
        self.directory = core.run_path(self.root, "fixture")
        self.directory.mkdir(parents=True)
        (self.directory / "logs").mkdir()
        core.atomic_json(self.stage / ".refresh-workspace.json", {"runId": "fixture", "ownerToken": "token"})
        self.value = {"schemaVersion": 1, "id": "fixture", "ownerToken": "token",
            "workspaceRoot": str(self.stage), "baselineRoot": str(self.root), "mode": "full",
            "phase": "ready", "codeFiles": {}, "codeNormalization": {}, "codeHash": core.digest({}),
            "codeCommit": "a" * 40, "codeDirty": False,
            "inputs": core.facts(core.source_paths(self.stage), self.stage), "sources": [],
            "imports": [], "attempts": [], "warnings": [], "status": "candidate_ready", "outcome": "success",
            "baselineArtifacts": {}, "plan": [{"id": "fixture"}],
            "steps": {"fixture": {"status": "success"}},
            "candidate": {"contentHash": "obsolete"}, "validation": {"status": "success", "artifacts": {}}}
        self.save()

    def save(self):
        core.save_run(self.directory, self.value)

    def observed(self):
        return core.read_json(self.directory / "run.json")

    def assert_failed(self, status=None, interrupted=False):
        value = self.observed()
        self.assertEqual(value["outcome"], "incomplete" if interrupted else "failed")
        if status:
            self.assertEqual(value["status"], status)
        self.assertNotIn("candidate", value)
        self.assertNotEqual(value.get("validation", {}).get("status"), "success")
        return value

    def incoming(self):
        return roster(Path(self.temp.name) / "raymond_james_2026-10-23.csv")

    def test_acquire_denial_is_failed_not_previous_success(self):
        with self.assertRaisesRegex(core.RefreshError, "allow-network"):
            acquire.acquire(self.root, "fixture", "act")
        value = self.assert_failed("failed")
        self.assertEqual(value["attempts"][-1]["status"], "failed")

    def test_import_retry_clears_failed_outcome_and_errors(self):
        with self.assertRaises(core.RefreshError):
            acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", Path(self.temp.name) / "missing")
        self.assert_failed("failed")
        value = acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", self.incoming())
        self.assertEqual((value["status"], value["outcome"]), ("ready", "success_with_warnings"))
        self.assertTrue(any("Initial snapshot" in warning for warning in value["warnings"]))
        self.assertNotIn("errorCode", value)
        self.assertNotIn("failedStep", value)

    def test_import_running_state_and_interrupt_are_incomplete(self):
        def interrupt(*args):
            self.assertEqual((self.observed()["status"], self.observed()["outcome"]), ("importing", "incomplete"))
            raise KeyboardInterrupt()
        with patch.object(acquire, "_prepare", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", self.incoming())
        value = self.assert_failed("incomplete", interrupted=True)
        self.assertEqual(value["attempts"][-1]["status"], "incomplete")
        value = acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", self.incoming())
        self.assertEqual(value["outcome"], "success_with_warnings")
        self.assertTrue(any("Initial snapshot" in warning for warning in value["warnings"]))

    def test_import_receipt_and_finish_errors_are_durable(self):
        for function in ("_receipt", "_finish"):
            self.value["inputs"] = core.facts(core.source_paths(self.stage), self.stage)
            self.save()
            with self.subTest(function=function), patch.object(acquire, function, side_effect=OSError("fixture")), self.assertRaises(OSError):
                acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", self.incoming())
            self.assert_failed("failed")

    def test_rejected_scope_and_lock_do_not_revoke_candidate(self):
        self.value["mode"] = "act-only"; self.save()
        with self.assertRaises(core.RefreshError):
            acquire.import_snapshot(self.root, "fixture", "forbes", self.incoming())
        self.assertEqual(self.observed()["candidate"], self.value["candidate"])
        with core.lock(self.directory), self.assertRaises(core.RefreshError):
            acquire.acquire(self.root, "fixture", "act")
        self.assertEqual(self.observed()["outcome"], "success")

    def test_build_preflight_failure_and_interruption_are_durable(self):
        for exception in (OSError("fixture"), SystemExit(2)):
            self.value["status"] = "candidate_ready"; self.save()
            with self.subTest(exception=exception), patch.object(core, "check_inputs", side_effect=exception), self.assertRaises(type(exception)):
                core.build(self.root, "fixture")
            self.assert_failed("incomplete" if isinstance(exception, SystemExit) else "failed",
                               interrupted=isinstance(exception, SystemExit))

    def test_build_dependency_and_output_facts_errors_are_durable(self):
        for plan, target in (([core.step("broken", [], dependencies=["absent"])], None),
                             ([core.step("broken", [], outputs=["data/output/missing"])], "facts")):
            self.value["status"] = "candidate_ready"; self.save()
            with self.subTest(target=target), patch.object(refresh_inventory, "validate_act_snapshot"), patch.object(core, "build_steps", return_value=plan):
                if target:
                    with patch.object(core, "facts", side_effect=OSError("stat fixture")), self.assertRaises(OSError):
                        core.build(self.root, "fixture")
                else:
                    with self.assertRaises(KeyError):
                        core.build(self.root, "fixture")
            self.assert_failed("failed")

    def test_report_baseline_error_revokes_prior_approval(self):
        self.value["baselineArtifacts"] = {"missing": {"bytes": 1, "sha256": "invalid"}}
        self.save()
        with self.assertRaisesRegex(core.RefreshError, "baseline changed"):
            release.report(self.root, "fixture")
        self.assert_failed("report_failed")

    def comparison(self):
        value = {"coverage": {"before": {}, "after": {}}, "semanticContentHash": "fixture",
                 "artifactAvailability": {"after": {}}, "assets": {"after": {"issues": []}}}
        core.atomic_json(self.directory / "reports/comparison.json", value)
        return value

    def command(self, stage, root, command, log):
        log.write_text("fixture", encoding="utf-8")

    def test_validate_early_report_error_is_failed(self):
        with patch.object(release, "_report", side_effect=OSError("baseline")), self.assertRaises(OSError):
            release.validate(self.root, "fixture", run_tests=False)
        value = self.assert_failed("validation_failed")
        self.assertEqual(value["validation"]["status"], "failed")

    def test_validate_running_interrupt_and_retry(self):
        with patch.object(release, "_report", side_effect=KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
            release.validate(self.root, "fixture", run_tests=False)
        self.assert_failed("incomplete", interrupted=True)
        # The supported resume rebuilds before revalidation.
        self.value = self.observed(); self.value["status"] = "built"; self.save()
        def check(*args):
            self.assertEqual(self.observed()["outcome"], "incomplete")
        with patch.object(release, "_report", side_effect=lambda *args: self.comparison()), patch.object(release, "execute_command", side_effect=self.command), patch.object(release, "write_descriptor", side_effect=check):
            value = release.validate(self.root, "fixture", run_tests=False)
        self.assertEqual((value["status"], value["outcome"]), ("validated", "success"))
        self.assertNotIn("errorCode", value)

    def test_validate_report_and_checks_share_one_lock(self):
        def compare(*args, **kwargs):
            # An importer trying to interleave at the comparison boundary must
            # fail before touching source snapshots or approval evidence.
            with self.assertRaisesRegex(core.RefreshError, "Another operation"):
                acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", self.incoming())
            return self.comparison()
        def command(*args):
            with self.assertRaisesRegex(core.RefreshError, "Another operation"):
                with core.lock(self.directory):
                    self.fail("Validation released its run lock")
            self.command(*args)
        with patch("refresh_reports.compare", side_effect=compare), patch.object(release, "report", side_effect=AssertionError("public report must not unlock")), patch.object(release, "execute_command", side_effect=command), patch.object(release, "write_descriptor"):
            value = release.validate(self.root, "fixture", run_tests=False)
        self.assertEqual(value["outcome"], "success")

    def test_prepare_preflight_failure_and_interruption_revoke_candidate(self):
        for exception in (OSError("fixture"), KeyboardInterrupt()):
            self.value["status"] = "candidate_ready"; self.save()
            with self.subTest(exception=exception), patch.object(release, "check_inputs", side_effect=exception), self.assertRaises(type(exception)):
                release.prepare(self.root, "fixture")
            self.assert_failed("incomplete" if isinstance(exception, KeyboardInterrupt) else "prepare_failed",
                               interrupted=isinstance(exception, KeyboardInterrupt))

    def test_prepare_archive_static_shard_and_final_verify_errors(self):
        registry = {"registryContentHash": "a" * 64}
        core.atomic_json(self.stage / "api/shared/approved-recipient-release.json", registry)
        core.atomic_json(self.stage / "data/identity/approved_recipients_manifest.json",
                         {"shards": {"one": {"blob": "approved_recipients/releases/" + "a" * 64 + "/01.json.gz"}}})
        shard = self.stage / "data/identity/approved_recipients_shards/01.json.gz"
        shard.parent.mkdir(parents=True, exist_ok=True); shard.write_bytes(b"fixture")
        def archive(path, files, base):
            path.write_bytes(b"fixture")
        for failure in ("archive", "static", "shard", "verify"):
            self.value["status"] = "candidate_ready"; self.save()
            deploy = Mock()
            if failure == "static":
                deploy.check_config.side_effect = core.RefreshError("static fixture")
            if failure == "shard":
                shard.unlink()
            elif not shard.exists():
                shard.write_bytes(b"fixture")
            with self.subTest(failure=failure), contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(release, "check_inputs"))
                stack.enter_context(patch.object(release, "paths_matching", return_value=[]))
                stack.enter_context(patch.object(release, "_archive", side_effect=core.RefreshError("archive fixture") if failure == "archive" else archive))
                stack.enter_context(patch.object(release.subprocess, "run", return_value=Mock(returncode=0)))
                stack.enter_context(patch.object(release, "_module", return_value=deploy))
                stack.enter_context(patch.object(release, "verify_release", side_effect=core.RefreshError("verify fixture") if failure == "verify" else None))
                with self.assertRaises(core.RefreshError):
                    release.prepare(self.root, "fixture")
                self.assert_failed("prepare_failed")

    def test_validate_prepare_success_preserves_warning_outcome_and_gates(self):
        self.value["warnings"] = ["reviewed fixture warning"]; self.save()
        registry = {"registryContentHash": "a" * 64}
        core.atomic_json(self.stage / "api/shared/approved-recipient-release.json", registry)
        core.atomic_json(self.stage / "data/identity/approved_recipients_manifest.json",
                         {"shards": {"one": {"blob": "approved_recipients/releases/" + "a" * 64 + "/01.json.gz"}}})
        shard = self.stage / "data/identity/approved_recipients_shards/01.json.gz"
        shard.parent.mkdir(parents=True, exist_ok=True); shard.write_bytes(b"fixture")
        def archive(path, files, base):
            self.assertEqual(self.observed()["outcome"], "incomplete")
            self.assertEqual(self.observed()["status"], "preparing")
            path.write_bytes(b"fixture")
        with patch.object(release, "_report", side_effect=lambda *args: self.comparison()), patch.object(release, "execute_command", side_effect=self.command), patch.object(release, "write_descriptor"), patch.object(release, "paths_matching", return_value=[]):
            validated = release.validate(self.root, "fixture", run_tests=False)
            self.assertEqual((validated["status"], validated["outcome"]), ("validated", "success_with_warnings"))
            with patch.object(release, "_archive", side_effect=archive), patch.object(release.subprocess, "run", return_value=Mock(returncode=0)), patch.object(release, "_module", return_value=Mock()):
                result = release.prepare(self.root, "fixture")
            self.assertTrue(result["verified"])
            self.assertEqual((self.observed()["status"], self.observed()["outcome"]), ("candidate_ready", "success_with_warnings"))
            # Genuine verifier gates are still active after the mocked packaging workflow.
            with self.assertRaisesRegex(core.RefreshError, "full regression"):
                release.verify_release(self.root, "fixture", production=True)
            archive_path = self.directory / "candidate/api.tgz"
            archive_path.write_bytes(b"tampered fixture")
            with self.assertRaisesRegex(core.RefreshError, "package or dependency changed"):
                release.verify_release(self.root, "fixture")

    def test_manifest_reload_rejects_interleaving_completed_import(self):
        # Simulate an import completing after validate's initial load_run read,
        # before it enters the lock. The stale built status must not approve it.
        original_lock = release.lock
        @contextlib.contextmanager
        def interleave(directory):
            changed = self.observed()
            changed.update(status="ready", outcome="success")
            changed.pop("candidate", None); changed.pop("validation", None)
            core.save_run(self.directory, changed)
            with original_lock(directory):
                yield
        with patch.object(release, "lock", interleave), self.assertRaisesRegex(core.RefreshError, "Build must finish"):
            release.validate(self.root, "fixture", run_tests=False)
        self.assertEqual(self.observed()["status"], "ready")
        self.assertEqual(self.observed()["outcome"], "success")

    def test_invalid_prepare_precondition_preserves_untouched_manifest(self):
        self.value["status"] = "ready"; self.save()
        before = self.observed()
        with self.assertRaises(core.RefreshError):
            release.prepare(self.root, "fixture")
        self.assertEqual(self.observed(), before)

    def test_warning_success_and_readonly_status(self):
        self.value["warnings"] = ["fixture warning"]; self.save()
        value = acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", self.incoming())
        self.assertEqual(value["outcome"], "success_with_warnings")
        before = self.observed()
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["--root", str(self.root), "status", "--run", "fixture"])
            cli.main(["--root", str(self.root), "build", "--run", "fixture", "--dry-run"])
        self.assertEqual(self.observed(), before)

    def test_successful_report_does_not_heal_unresolved_operation_failure(self):
        for status in ("validation_failed", "report_failed", "prepare_failed"):
            self.value.update(status=status, outcome="success", errorCode="PRIOR_FAILURE", failedStep="prior")
            self.save()
            with self.subTest(status=status), patch.object(release, "_report", side_effect=lambda *args: self.comparison()):
                release.report(self.root, "fixture")
                observed = self.observed()
                self.assertEqual((observed["status"], observed["outcome"]), (status, "failed"))
                self.assertEqual(observed["errorCode"], "PRIOR_FAILURE")
                self.assertEqual(observed["failedStep"], "prior")

    def test_successful_report_preserves_intact_candidate_approval(self):
        before = self.observed()
        with patch.object(release, "_report", side_effect=lambda *args: self.comparison()):
            release.report(self.root, "fixture")
        observed = self.observed()
        for key in ("status", "outcome", "validation", "candidate"):
            self.assertEqual(observed[key], before[key])

    def test_unresolved_source_attempt_requires_retry_or_reviewed_fallback(self):
        for status in ("failed", "incomplete", "running"):
            self.value["attempts"] = [{"sourceId": "act", "status": status}]
            self.value.update(status="built", outcome="success")
            self.save()
            with self.subTest(status=status), patch.object(refresh_inventory, "validate_act_snapshot"):
                with self.assertRaisesRegex(core.RefreshError, "fallback-reason"):
                    core.build(self.root, "fixture")
                self.assert_failed("failed")
                with patch.object(core, "build_steps", return_value=[]), patch.object(refresh_inventory, "inventory", return_value=[]), patch.object(refresh_inventory, "candidate_inventory", return_value=[]):
                    value = core.build(self.root, "fixture", fallback_reason="Reviewed retained fixture source")
                self.assertEqual(value["outcome"], "success_with_warnings")
                self.assertTrue(value["fallbacks"])

    def test_missing_private_dependencies_before_validation_fail_closed(self):
        required = list(release.REQUIRED_PRIVATE) + ["data/identity/approved_recipients_shards/01.json.gz"]
        for relative in required:
            private_fixture(self.stage)
            self.save()
            (self.stage / relative).unlink()
            with self.subTest(relative=relative), self.assertRaisesRegex(core.RefreshError, "Required"):
                release.validate(self.root, "fixture", run_tests=False)
            self.assert_failed("validation_failed")

    def test_dependency_lost_during_validation_is_not_silently_omitted(self):
        def descriptor(*args):
            (self.stage / "data/output/advisor_emails.json.gz").unlink()
        with patch.object(release, "_report", side_effect=lambda *args: self.comparison()), patch.object(release, "execute_command", side_effect=self.command), patch.object(release, "write_descriptor", side_effect=descriptor), self.assertRaisesRegex(core.RefreshError, "advisor_emails"):
            release.validate(self.root, "fixture", run_tests=False)
        self.assert_failed("validation_failed")

    def test_missing_activity_lookup_before_prepare_fails_closed(self):
        (self.stage / "data/output/advisor_emails.json.gz").unlink()
        with self.assertRaisesRegex(core.RefreshError, "advisor_emails"):
            release.prepare(self.root, "fixture")
        self.assert_failed("prepare_failed")

    def test_resume_cli_uses_final_validation_outcome(self):
        with patch.object(cli, "load_run", return_value=(self.directory, {"phase": "ready"}, self.stage)), patch.object(cli, "build", return_value={"id": "fixture", "status": "built", "outcome": "success", "warnings": []}), patch.object(release, "validate", return_value={"status": "validated", "outcome": "success_with_warnings", "warnings": ["reviewed"]}), contextlib.redirect_stdout(io.StringIO()) as output:
            cli.main(["--root", str(self.root), "resume", "--run", "fixture", "--through", "validate"])
        self.assertEqual(json.loads(output.getvalue())["outcome"], "success_with_warnings")


if __name__ == "__main__":
    unittest.main()
