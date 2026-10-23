"""Retry attestation tests: synthetic fixtures and mocked provider only."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import refresh_core as core
import refresh_repair_runner as runner
import refresh_repair_retry as retry
import test_refresh_repair_runner as fixtures


class RetryAuthorizationTests(unittest.TestCase):
    def setUp(self):
        fixtures.TextOnlyRepairRunnerTests.setUp(self)

    def propose(self):
        return runner.run(self.stage, self.request["id"], self.fixture, **self.options)

    def fail(self):
        with patch.object(runner, "request_model", side_effect=core.RefreshError("Synthetic failure")):
            with self.assertRaises(core.RefreshError): self.propose()

    def authorize(self, **overrides):
        return retry.authorize(self.stage, self.request["id"], **{
            "operator": "Retry operator", "reason": "Reviewed prior failure and approved one more attempt",
            "acknowledge_possible_charge": True, **overrides})

    def test_explicit_single_use_retry_preserves_failed_attempt_and_authorization(self):
        self.fail()
        before = core.read_json(self.folder / "request.json")["attempts"]
        with patch.object(runner, "request_model") as provider:
            decision = self.authorize()
            provider.assert_not_called()
        self.assertEqual(decision["previousAttemptsHash"], core.digest(before))
        with patch.object(runner, "request_model", return_value=fixtures.response()) as provider:
            self.propose()
            with self.assertRaises(core.RefreshError): self.propose()
            self.assertEqual(provider.call_count, 1)
        current = core.read_json(self.folder / "request.json")
        self.assertEqual(current["attempts"][0], before[0])
        self.assertEqual(len(current["attempts"]), 2)
        self.assertEqual(current["attempts"][1]["status"], "success")
        self.assertTrue(current["attempts"][1]["retryAuthorizationHash"])
        self.assertEqual(len(list(self.folder.glob("attempt-*-fixture.json"))), 2)

    def test_retry_requires_named_authorizer_reason_and_charge_acknowledgment(self):
        self.fail()
        for options in ({"operator": ""}, {"reason": "short"}, {"acknowledge_possible_charge": False}):
            with self.assertRaises(core.RefreshError): self.authorize(**options)
        self.assertEqual(core.read_json(self.folder / "request.json")["status"], "proposal_failed")

    def test_three_total_attempts_is_hard_bound(self):
        for number in range(3):
            if number: self.authorize()
            self.fail()
        with self.assertRaisesRegex(core.RefreshError, "maximum three"):
            self.authorize()
        self.assertEqual(len(core.read_json(self.folder / "request.json")["attempts"]), 3)

    def test_tampered_authorization_or_history_blocks_provider(self):
        self.fail(); self.authorize()
        file = self.folder / "retry-authorization-2.json"
        original = file.read_bytes()
        file.write_text('{}', encoding="utf-8")
        with patch.object(runner, "request_model") as provider:
            with self.assertRaises(core.RefreshError): self.propose()
            provider.assert_not_called()
        file.write_bytes(original)
        request_file = self.folder / "request.json"
        value = core.read_json(request_file)
        value["attempts"][0]["finishedUtc"] = "altered"
        core.atomic_json(request_file, value)
        with patch.object(runner, "request_model") as provider:
            with self.assertRaisesRegex(core.RefreshError, "history"): self.propose()
            provider.assert_not_called()

    def test_running_or_completed_proposals_cannot_be_reauthorized(self):
        with self.assertRaises(core.RefreshError): self.authorize()
        with patch.object(runner, "request_model", return_value=fixtures.response()): self.propose()
        with self.assertRaises(core.RefreshError): self.authorize()

    def test_duplicate_approval_and_changed_source_are_refused(self):
        self.fail(); self.authorize()
        file = self.folder / "retry-authorization-2.json"
        before = file.read_bytes()
        with self.assertRaises(core.RefreshError): self.authorize()
        self.assertEqual(file.read_bytes(), before)
        self.adapter.write_text(self.adapter.read_text() + '\n# changed\n', encoding="utf-8")
        with patch.object(runner, "request_model") as provider:
            with self.assertRaises(core.RefreshError): self.propose()
            provider.assert_not_called()

    def test_failed_attempt_with_existing_proposal_artifact_cannot_retry(self):
        self.fail()
        core.atomic_json(self.folder / "model-proposal.json", {"evidence": "do not overwrite"})
        with self.assertRaises(core.RefreshError): self.authorize()


if __name__ == "__main__":
    unittest.main()
