"""Real restricted subprocesses; no provider calls, source fetches or live writes."""
import ast
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import refresh_core as core
import refresh_repair as repair
import refresh_repair_runner as runner
import refresh_repair_validation as validation
import refresh_repair_worker as worker
import refresh_repair_decision as decision
import refresh_process_limits as limits
from test_refresh_repair_runner import response

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / repair.ADAPTER).read_text(encoding="utf-8-sig")
EMAIL_FUNCTION = next(ast.get_source_segment(SOURCE, n) for n in ast.parse(SOURCE).body
                      if isinstance(n, ast.FunctionDef) and n.name == "extract_emails")
FIXED_FUNCTION = EMAIL_FUNCTION.replace('    if not nodes:\n        return [], "field_missing"',
    '    if not nodes:\n        nodes = tree.xpath(\'//input[@name="field_email_override_v2"]/@value\')\n'
    '    if not nodes:\n        return [], "field_missing"')


class TrustedParserValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.stage = Path(self.temp.name)
        self.adapter = self.stage / repair.ADAPTER
        self.adapter.parent.mkdir(parents=True); self.adapter.write_text(SOURCE, encoding="utf-8")
        self.report = {"sourceId": repair.SOURCE, "policyVersion": "edward-jones-history-v1", "status": "held",
                       "candidateSha256": "a" * 64, "baselineSha256": "b" * 64,
                       "issues": [{"code": "FIELD_VALUES_LOST", "field": "email", "count": 50}]}
        self.request = repair.queue_request(self.stage, self.report)
        self.folder = self.stage / "data/refresh/repair_requests" / self.request["id"]
        self.failure = self.stage / "failure.json"
        core.atomic_json(self.failure, {"kind": "synthetic", "cases": [{
            "html": '<input name="field_email_override_v2" value="new@example.invalid">',
            "expectedEmails": ["new@example.invalid"], "expectedBio": None}]})
        self.regression = self.stage / "regression.json"
        core.atomic_json(self.regression, {"kind": "synthetic", "cases": [{
            "html": '<html><input name="field_email_override" value="held-out@example.invalid"></html>',
            "expectedEmails": ["held-out@example.invalid"], "expectedBio": None}]})

    def propose(self, function=FIXED_FUNCTION):
        proposed = response(changes=[{"function": "extract_emails", "source": function}])
        with patch.object(runner, "request_model", return_value=proposed):
            runner.run(self.stage, self.request["id"], self.failure, model="fixture-model",
                       operator="Fixture submitter", reason="Approved synthetic fixture for parser diagnosis",
                       allow_ai=True, confirm_fixture=True)

    def validate(self, **kwargs):
        return validation.validate(self.stage, self.request["id"], self.regression, **kwargs)

    def test_actual_parser_repair_fixes_failure_and_preserves_hidden_regressions(self):
        self.assertNotEqual(EMAIL_FUNCTION, FIXED_FUNCTION)
        self.propose(); before = self.adapter.read_bytes()
        proof = self.validate()
        self.assertEqual(proof["status"], "passed")
        self.assertTrue(proof["result"]["failureReproduced"])
        self.assertTrue(proof["result"]["failureFixed"])
        self.assertTrue(proof["result"]["regressionsPassed"])
        self.assertGreaterEqual(len(proof["result"]["afterRegression"]), 10)
        self.assertEqual(self.adapter.read_bytes(), before)
        receipt = core.read_json(next((self.folder / "logs").glob("*.process.json")))
        self.assertEqual(receipt["status"], "exited")
        self.assertEqual(receipt["exitCode"], 0)
        self.assertFalse(core.active_children(self.folder))

    def test_proposed_file_access_is_rejected_without_creating_file(self):
        marker = self.stage / "must-not-exist.txt"
        function = EMAIL_FUNCTION.splitlines()[0] + '\n    open(' + repr(str(marker)) + ', "w")\n    return [], "ok"\n'
        self.propose(function)
        proof = self.validate()
        self.assertEqual(proof["status"], "failed")
        self.assertEqual(proof["result"]["errorCode"], "RESTRICTED_PARSER_TEST_FAILED")
        self.assertFalse(marker.exists())
        self.assertEqual(self.adapter.read_text(encoding="utf-8"), SOURCE)

    def test_mandatory_historical_tests_reject_new_format_only_patch(self):
        function = EMAIL_FUNCTION.replace('name="field_email_override"', 'name="field_email_override_v2"')
        self.propose(function)
        proof = self.validate()
        self.assertEqual(proof["status"], "failed")
        self.assertTrue(proof["result"]["failureFixed"])
        self.assertFalse(proof["result"]["regressionsPassed"])

    def test_no_reproduced_failure_cannot_be_called_a_verified_fix(self):
        core.atomic_json(self.failure, {"kind": "synthetic", "cases": [{
            "html": '<input name="field_email_override" value="already@example.invalid">',
            "expectedEmails": ["already@example.invalid"], "expectedBio": None}]})
        self.propose()
        proof = self.validate()
        self.assertEqual(proof["status"], "failed")
        self.assertFalse(proof["result"]["failureReproduced"])

    def test_overlap_with_model_visible_fixture_is_refused(self):
        self.propose()
        self.regression.write_bytes(self.failure.read_bytes())
        with self.assertRaisesRegex(core.RefreshError, "must differ"):
            self.validate()

    def test_partial_case_overlap_is_refused(self):
        self.propose()
        fixture = core.read_json(self.regression)
        fixture["cases"].extend(core.read_json(self.failure)["cases"])
        core.atomic_json(self.regression, fixture)
        with self.assertRaisesRegex(core.RefreshError, "must not overlap"):
            self.validate()

    def test_proposal_or_source_tamper_is_refused(self):
        self.propose()
        proposal_file = self.folder / "proposal.json"
        with proposal_file.open("a") as handle: handle.write("\n")
        with self.assertRaisesRegex(core.RefreshError, "proposal hash"):
            self.validate()

    def test_source_change_is_refused(self):
        self.propose()
        with self.adapter.open("a") as handle: handle.write("\n# changed\n")
        with self.assertRaisesRegex(core.RefreshError, "source changed"):
            self.validate()

    def test_failure_fixture_tamper_is_refused(self):
        self.propose()
        core.atomic_json(self.folder / "approved-fixture.json", core.read_json(self.regression))
        with self.assertRaisesRegex(core.RefreshError, "fixture no longer binds"):
            self.validate()

    def test_timeout_kills_actual_worker_and_records_terminal_failure(self):
        core.atomic_json(self.failure, {"kind": "synthetic", "cases": [{
            "html": "a" * 5000 + "!", "expectedEmails": [], "expectedBio": None}]})
        function = EMAIL_FUNCTION.splitlines()[0] + '\n    re.sub("(a+)+$", "", page_html)\n    return [], "field_missing"\n'
        self.propose(function)
        with self.assertRaisesRegex(core.RefreshError, "wall-time limit"):
            self.validate(timeout_seconds=1)
        current = core.read_json(self.folder / "request.json")
        self.assertEqual(current["validation"]["status"], "failed")
        receipt = core.read_json(next((self.folder / "logs").glob("*.process.json")))
        self.assertEqual(receipt["status"], "exited")
        self.assertFalse(core.active_children(self.folder))

    def test_worker_does_not_inherit_provider_or_production_secrets(self):
        self.propose()
        real_popen = validation.subprocess.Popen
        def spawn(*args, **kwargs):
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertNotIn("AZURE_STORAGE_CONNECTION_STRING", kwargs["env"])
            self.assertNotIn("PYTHONPATH", kwargs["env"])
            self.assertIn("-I", args[0])
            self.assertNotEqual(Path(kwargs["cwd"]), self.stage)
            return real_popen(*args, **kwargs)
        with patch.dict("os.environ", {"OPENAI_API_KEY": "fixture-secret", "AZURE_STORAGE_CONNECTION_STRING": "fixture-secret"}), \
             patch.object(validation.subprocess, "Popen", side_effect=spawn):
            self.assertEqual(self.validate()["status"], "passed")

    def test_worker_spawn_error_is_persisted_without_live_handle(self):
        self.propose()
        with patch.object(validation.subprocess, "Popen", side_effect=OSError("Fixture launch failure")):
            with self.assertRaises(OSError): self.validate()
        self.assertEqual(core.read_json(self.folder / "request.json")["validation"]["status"], "failed")
        self.assertFalse(core.active_children(self.folder))

    def test_resource_limit_setup_failure_kills_worker_before_proposal_input(self):
        self.propose()
        with patch.object(validation, "attach", side_effect=core.RefreshError("Fixture containment failure")):
            with self.assertRaisesRegex(core.RefreshError, "containment failure"):
                self.validate()
        receipt = core.read_json(next((self.folder / "logs").glob("*.process.json")))
        self.assertEqual(receipt["status"], "exited")
        identity = core.process_identity(receipt["pid"])
        self.assertFalse(identity["alive"] and identity["created"] == receipt["created"])

    def test_restricted_language_refuses_escape_primitives(self):
        bodies = [
            'open("secret")', '__import__("os")', 'getattr(str, "__class__")',
            'html.fromstring.__globals__', '"{0.__class__}".format(1)',
            'html.fromstring = str', 'eval("1+1")', 'exec("print(1)")',
        ]
        for body in bodies:
            function = EMAIL_FUNCTION.splitlines()[0] + "\n    " + body + '\n    return [], "ok"\n'
            replacement = runner._replacement(SOURCE, [{"function": "extract_emails", "source": function}])
            with self.assertRaises(ValueError, msg=body): worker.restricted_namespace(replacement)

    def test_top_level_scraper_code_is_never_imported_or_executed(self):
        marker = self.stage / "top-level-must-not-exist.txt"
        source = SOURCE + '\nopen(' + repr(str(marker)) + ', "w")\n'
        namespace = worker.restricted_namespace(source)
        self.assertTrue(all(item["passed"] for item in worker.check_cases(namespace, worker.BUILTIN_REGRESSION)))
        self.assertFalse(marker.exists())

    def approve(self):
        return decision.decide(self.stage, self.request["id"], decision="approve",
                               reviewer="Independent fixture reviewer", reason="Inspected the parser diff and independent regression evidence")

    def test_independent_review_and_export_never_apply_source(self):
        self.propose(); self.validate()
        before = self.adapter.read_bytes()
        approved = self.approve()
        self.assertFalse(approved["canApplyPatch"])
        result = decision.export(self.stage, self.request["id"])
        self.assertFalse(result["sourceModified"])
        self.assertFalse(result["publicationAuthorized"])
        self.assertTrue(result["requiresNewRefreshRun"])
        self.assertEqual(self.adapter.read_bytes(), before)
        exported = core.read_json(self.folder / "approved-change.json")
        self.assertIn("field_email_override_v2", exported["unifiedDiff"])
        self.assertEqual(decision.export(self.stage, self.request["id"]), result)

    def test_submitter_cannot_approve_own_proposal(self):
        self.propose(); self.validate()
        with self.assertRaisesRegex(core.RefreshError, "own repair"):
            decision.decide(self.stage, self.request["id"], decision="approve",
                            reviewer=" fixture SUBMITTER ", reason="This is not independent review")
        self.assertFalse((self.folder / "repair-decision.json").exists())

    def test_unvalidated_or_failed_proposal_cannot_be_approved_or_exported(self):
        self.propose()
        with self.assertRaisesRegex(core.RefreshError, "No trusted"):
            self.approve()
        with self.assertRaisesRegex(core.RefreshError, "No unchanged independent approval"):
            decision.export(self.stage, self.request["id"])

    def test_failed_regressions_block_approval(self):
        self.propose(EMAIL_FUNCTION.replace('name="field_email_override"', 'name="field_email_override_v2"'))
        self.assertEqual(self.validate()["status"], "failed")
        with self.assertRaisesRegex(core.RefreshError, "no matching passed"):
            self.approve()

    def test_rejection_is_immutable_and_cannot_export(self):
        self.propose()
        rejected = decision.decide(self.stage, self.request["id"], decision="reject",
                                  reviewer="Independent fixture reviewer", reason="Insufficient evidence for a parser repair")
        self.assertFalse(rejected["authorizesExportOnly"])
        with self.assertRaisesRegex(core.RefreshError, "immutable"):
            self.approve()
        with self.assertRaisesRegex(core.RefreshError, "No unchanged independent approval"):
            decision.export(self.stage, self.request["id"])

    def test_validation_evidence_tamper_blocks_review(self):
        self.propose(); self.validate()
        pointer = core.read_json(self.folder / "request.json")["validationEvidence"]
        with (self.folder / pointer["file"]).open("a") as handle: handle.write("\n")
        with self.assertRaisesRegex(core.RefreshError, "validation evidence changed"):
            self.approve()

    def test_held_out_fixture_tamper_blocks_review(self):
        self.propose(); self.validate()
        file = self.folder / core.read_json(self.folder / "request.json")["validation"]["regressionFixtureFile"]
        changed = core.read_json(file); changed["cases"][0]["html"] += " altered"
        core.atomic_json(file, changed)
        with self.assertRaisesRegex(core.RefreshError, "regression fixture changed"):
            self.approve()

    def test_changed_reviewer_decision_cannot_be_exported(self):
        self.propose(); self.validate(); self.approve()
        file = self.folder / "repair-decision.json"
        changed = core.read_json(file); changed["reviewer"] = "Someone else"
        core.atomic_json(file, changed)
        with self.assertRaisesRegex(core.RefreshError, "No unchanged independent approval"):
            decision.export(self.stage, self.request["id"])

    def test_stale_worker_version_invalidates_validation(self):
        self.propose(); self.validate()
        file = self.folder / "request.json"
        current = core.read_json(file)
        changed = dict(current["validation"]); changed["workerSha256"] = "a" * 64
        evidence = self.folder / current["validationEvidence"]["file"]
        core.atomic_json(evidence, changed)
        current["validation"] = changed; current["validationEvidence"]["sha256"] = core.file_hash(evidence)
        core.atomic_json(file, current)
        with self.assertRaisesRegex(core.RefreshError, "implementation changed"):
            self.approve()

    @unittest.skipUnless(os.name == "nt", "Native Windows job limit proof")
    def test_actual_windows_job_denies_memory_over_limit(self):
        import subprocess
        code = ('import sys\nsys.stdin.readline()\ntry:\n data=bytearray(400*1024*1024)\n'
                ' print("UNLIMITED")\nexcept MemoryError:\n print("LIMITED")\n')
        process = subprocess.Popen([sys.executable, "-I", "-c", code], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        close = None
        try:
            close = limits.attach(process)
            out, err = process.communicate(input=b"go\n", timeout=10)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(out.strip(), b"LIMITED")
        finally:
            if process.poll() is None:
                process.kill(); process.communicate()
            if close is not None: close()

    @unittest.skipUnless(os.name == "nt", "Native Windows supervisor exit containment")
    def test_actual_windows_job_close_terminates_child(self):
        import subprocess
        process = subprocess.Popen([sys.executable, "-I", "-c", "import sys; sys.stdin.readline()"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        close = None
        try:
            close = limits.attach(process)
            close()
            process.wait(timeout=5)
            self.assertFalse(core.process_identity(process.pid)["alive"])
        finally:
            if process.poll() is None: process.kill()
            process.communicate()
            if close is not None: close()


if __name__ == "__main__":
    unittest.main()
