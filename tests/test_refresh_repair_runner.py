"""Text-only AI transport is mocked; no secrets read or provider calls made."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import refresh_core as core
import refresh_repair as repair
import refresh_repair_runner as runner
from test_refresh_repair import SOURCE


def response(changes=None, summary="Adapt the parser to the new fixture markup"):
    if changes is None:
        changes = [{"function": "extract_emails", "source": 'def extract_emails(page_html):\n    return [], "field_empty"\n'}]
    return {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps({"summary": summary, "changes": changes})}]}]}


class TextOnlyRepairRunnerTests(unittest.TestCase):
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
        self.fixture = self.stage / "fixture.json"
        core.atomic_json(self.fixture, {"kind": "synthetic", "cases": [
            {"html": "<html>synthetic fixture</html>", "expectedEmails": [], "expectedBio": None}]})
        self.options = {"model": "operator-selected-model", "operator": "Fixture operator",
                        "reason": "Approved synthetic fixture to reproduce the parser failure",
                        "allow_ai": True, "confirm_fixture": True}

    def run_case(self, **overrides):
        return runner.run(self.stage, self.request["id"], self.fixture, **{**self.options, **overrides})

    def test_authorization_required_before_transport_and_attempt(self):
        with patch.object(runner, "request_model") as call:
            for options in ({"allow_ai": False}, {"confirm_fixture": False}, {"model": ""},
                            {"operator": ""}, {"reason": "short"}):
                with self.assertRaises(core.RefreshError): self.run_case(**options)
            call.assert_not_called()
        self.assertFalse(core.read_json(self.folder / "request.json")["attempts"])

    def test_success_is_tool_free_bounded_and_does_not_apply_source(self):
        original = self.adapter.read_bytes()
        def provider(body):
            self.assertEqual(body["tools"], [])
            self.assertEqual(body["tool_choice"], "none")
            self.assertFalse(body["store"])
            self.assertEqual(body["max_output_tokens"], 6000)
            self.assertEqual(body["text"]["format"]["type"], "json_schema")
            current = core.read_json(self.folder / "request.json")
            self.assertEqual(current["attempts"][0]["status"], "running")
            self.assertTrue(current["modelAuthorized"])
            return response()
        with patch.object(runner, "request_model", side_effect=provider):
            result = self.run_case()
        self.assertEqual(result["status"], "awaiting_validation_and_independent_review")
        self.assertEqual(self.adapter.read_bytes(), original)
        self.assertEqual(core.read_json(self.folder / "request.json")["attempts"][0]["status"], "success")
        self.assertFalse(result["canApplyPatch"])

    def test_no_environment_or_other_data_in_prompt(self):
        (self.stage / "private-act-export.json").write_text("PRIVATE_ACT_SECRET", encoding="utf-8")
        def provider(body):
            text = json.dumps(body)
            self.assertNotIn("PRIVATE_ACT_SECRET", text)
            self.assertNotIn("PRIVATE_ENV_TOKEN", text)
            self.assertNotIn(str(self.stage), text)
            return response()
        with patch.dict("os.environ", {"AZURE_STORAGE_CONNECTION_STRING": "PRIVATE_ENV_TOKEN"}), \
             patch.object(runner, "request_model", side_effect=provider):
            self.run_case()

    def test_failure_consumes_single_attempt_and_preserves_source(self):
        with patch.object(runner, "request_model", side_effect=core.RefreshError("Fixture transport failure")) as call:
            with self.assertRaises(core.RefreshError): self.run_case()
            with self.assertRaisesRegex(core.RefreshError, "consumed"):
                self.run_case()
            self.assertEqual(call.call_count, 1)
        self.assertEqual(core.read_json(self.folder / "request.json")["status"], "proposal_failed")
        self.assertEqual(self.adapter.read_text(encoding="utf-8"), SOURCE)

    def test_interrupted_attempt_is_not_silently_retried(self):
        with patch.object(runner, "request_model", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.run_case()
        self.assertEqual(core.read_json(self.folder / "request.json")["attempts"][0]["status"], "incomplete")
        with patch.object(runner, "request_model") as call:
            with self.assertRaises(core.RefreshError): self.run_case()
            call.assert_not_called()

    def test_provider_cannot_modify_validation_or_imports(self):
        value = response(changes=[{"function": "validate_discovery", "source": "def validate_discovery(rows):\n return rows\n"}])
        with patch.object(runner, "request_model", return_value=value):
            with self.assertRaises(core.RefreshError): self.run_case()
        self.assertFalse((self.folder / "proposal.json").exists())

    def test_provider_cannot_change_signature_or_add_module_side_effect(self):
        for text in ('def extract_emails(page_html=open("secret")):\n return [], "ok"\n',
                     'def extract_emails(page_html):\n return [], "ok"\nprint("side effect")\n'):
            with self.assertRaises((core.RefreshError, SyntaxError)):
                replacement = runner._replacement(SOURCE, [{"function": "extract_emails", "source": text}])
                if repair._outside_parser_ast(SOURCE) != repair._outside_parser_ast(replacement):
                    raise core.RefreshError("Protected boundary changed")

    def test_incomplete_refused_empty_and_invalid_responses_fail_closed(self):
        values = [{"status": "incomplete", "output": []}, {"status": "completed", "output": []},
                  {"status": "completed", "output": [{"type": "function_call"}]},
                  {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
                  {"status": "completed", "output": None}, {"status": "completed", "output": [None]},
                  {"status": "completed", "output": [{"type": "message", "content": None}]}]
        for value in values:
            with self.assertRaises(core.RefreshError): runner._parse_response(value)

    def test_semantic_noop_is_failed_not_reported_as_success(self):
        unchanged = [{"function": "extract_emails", "source": 'def extract_emails(page_html):\n    return [], "field_missing"\n'}]
        with patch.object(runner, "request_model", return_value=response(changes=unchanged)):
            with self.assertRaisesRegex(core.RefreshError, "no semantic change"):
                self.run_case()
        request = core.read_json(self.folder / "request.json")
        self.assertEqual(request["attempts"][0]["status"], "failed")
        self.assertEqual(request["status"], "proposal_failed")
        self.assertFalse((self.folder / "model-proposal.json").exists())

    def test_duplicate_empty_and_unexpected_function_edits_are_rejected(self):
        item = {"function": "extract_emails", "source": 'def extract_emails(page_html):\n return [], "ok"\n'}
        for changes in ([], [item, item], [{**item, "function": "validate_discovery"}]):
            with self.assertRaises(core.RefreshError): runner._replacement(SOURCE, changes)

    def test_non_public_fixture_and_extra_fields_refused_before_provider(self):
        for fixture in ({"kind": "act-export", "cases": []}, {"kind": "synthetic", "cases": [], "cookies": "secret"}):
            core.atomic_json(self.fixture, fixture)
            with patch.object(runner, "request_model") as call:
                with self.assertRaises(core.RefreshError): self.run_case()
                call.assert_not_called()

    def test_adapter_change_before_or_during_call_is_rejected(self):
        def provider(body):
            self.adapter.write_text(SOURCE + "\n# changed\n", encoding="utf-8")
            return response()
        with patch.object(runner, "request_model", side_effect=provider):
            with self.assertRaisesRegex(core.RefreshError, "changed during"):
                self.run_case()
        self.assertFalse((self.folder / "proposal.json").exists())

    def test_http_transport_fixed_origin_no_proxy_no_redirect(self):
        from unittest.mock import Mock
        import refresh_repair_transport as transport
        http_response = Mock(status_code=401)
        with patch("httpx.Client") as factory:
            factory.return_value.__enter__.return_value.stream.return_value.__enter__.return_value = http_response
            self.assertEqual(transport.request({"fixture": True}, "fixture-not-a-real-key"),
                             {"error": "HTTP_STATUS", "httpStatus": 401})
            self.assertFalse(factory.call_args.kwargs["follow_redirects"])
            self.assertFalse(factory.call_args.kwargs["trust_env"])
            post = factory.return_value.__enter__.return_value.stream
            self.assertEqual(post.call_args.args[:2], ("POST", "https://api.openai.com/v1/responses"))
            http_response.json.assert_not_called()

    def test_provider_deadline_kills_and_reaps_real_child_without_network(self):
        import subprocess
        import time
        real_popen = subprocess.Popen
        children = []
        def spawn(command, **kwargs):
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertNotIn("AZURE_STORAGE_CONNECTION_STRING", kwargs["env"])
            process = real_popen([sys.executable, "-I", "-B", "-c",
                                 "import sys,time;sys.stdin.buffer.read();time.sleep(30)"], **kwargs)
            children.append(process)
            return process
        started = time.monotonic()
        with patch.object(runner.subprocess, "Popen", side_effect=spawn):
            with self.assertRaisesRegex(core.RefreshError, "wall-time"):
                runner._provider_process({"body": {}, "token": "synthetic-key"}, timeout_seconds=1)
        self.assertLess(time.monotonic() - started, 8)
        self.assertIsNotNone(children[0].poll())

    def test_provider_child_success_and_failure_are_checked_without_network(self):
        import subprocess
        real_popen = subprocess.Popen
        for output, passes in (({"response": response()}, True), ({"error": "TRANSPORT_FAILED"}, False)):
            def spawn(command, **kwargs):
                script = "import sys;sys.stdin.buffer.read();print(" + repr(json.dumps(output)) + ")"
                return real_popen([sys.executable, "-I", "-B", "-c", script], **kwargs)
            with patch.object(runner.subprocess, "Popen", side_effect=spawn):
                if passes:
                    self.assertEqual(runner._provider_process({"body": {}, "token": "synthetic"}), response())
                else:
                    with self.assertRaises(core.RefreshError):
                        runner._provider_process({"body": {}, "token": "synthetic"})


if __name__ == "__main__":
    unittest.main()
