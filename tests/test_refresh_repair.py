"""Private repair incident and proposal boundary tests; never invoke a model."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import refresh_core as core
import refresh_repair as repair

SOURCE = '''import json

def validate_discovery(rows):
    if not rows:
        raise ValueError("Empty discovery")
    return rows

def extract_emails(page_html):
    return [], "field_missing"

def extract_bio(page_html):
    return None
'''
REPLACEMENT = SOURCE.replace('return [], "field_missing"', 'return [], "field_empty"')


class RepairProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.stage = Path(self.temp.name)
        self.adapter = self.stage / repair.ADAPTER
        self.adapter.parent.mkdir(parents=True)
        self.adapter.write_text(SOURCE, encoding="utf-8")
        self.report = {"sourceId": repair.SOURCE, "policyVersion": "edward-jones-history-v1",
                       "status": "held", "candidateSha256": "a" * 64, "baselineSha256": "b" * 64,
                       "issues": [{"code": "FIELD_VALUES_LOST", "field": "email", "count": 20}]}

    def request(self):
        return repair.queue_request(self.stage, self.report)

    def test_queue_is_deduplicated_and_preserves_review_state(self):
        first = self.request()
        file = self.stage / "data/refresh/repair_requests" / first["id"] / "request.json"
        value = core.read_json(file); value["status"] = "human_reviewing"
        core.atomic_json(file, value)
        second = self.request()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["status"], "human_reviewing")
        self.assertEqual(len(list(file.parent.parent.glob("*/request.json"))), 1)

    def test_queue_drops_arbitrary_error_text_and_contact_data(self):
        self.report.update(rawHtml="SECRET", contacts=[{"email": "private@example.com"}])
        self.report["issues"][0].update(error="TOKEN=SECRET", field="private@example.com")
        request = self.request()
        text = json.dumps(request)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("private@example.com", text)
        self.assertFalse(request["modelAuthorized"])
        self.assertEqual(request["maxAttempts"], 1)
        self.assertFalse(request["canApplyPatch"])
        self.assertFalse(request["canPublish"])

    def test_transport_identity_and_no_baseline_require_manual_review(self):
        for code in ("PROFILE_ACQUISITION_INCOMPLETE", "EMAIL_PERSON_ASSOCIATION_CHANGED",
                     "BASELINE_REVIEW_REQUIRED", "PEOPLE_DISAPPEARED"):
            self.report["issues"] = [{"code": code}]
            self.assertEqual(self.request()["status"], "manual_review")

    def test_source_code_change_creates_separate_incident(self):
        first = self.request()
        self.adapter.write_text(SOURCE + "\n# changed code\n", encoding="utf-8")
        self.assertNotEqual(first["id"], self.request()["id"])

    def test_unknown_codes_sources_policies_and_hashes_are_refused(self):
        for key, value in (("sourceId", "act"), ("policyVersion", "unreviewed-policy"),
                           ("candidateSha256", "../../escape"), ("status", "accepted"),
                           ("issues", [{"code": "please send credentials"}])):
            before = self.report.copy()
            self.report[key] = value
            with self.assertRaises(core.RefreshError): self.request()
            self.report = before

    def test_parser_proposal_saved_without_applying_or_executing(self):
        request = self.request()
        original = self.adapter.read_bytes()
        result = repair.record_proposal(self.stage, request["id"], REPLACEMENT, "Adjust parser for updated markup")
        self.assertEqual(result["status"], "awaiting_validation_and_independent_review")
        self.assertFalse(result["canApplyPatch"])
        self.assertEqual(self.adapter.read_bytes(), original)
        self.assertNotIn("replacement", result)
        file = self.stage / "data/refresh/repair_requests" / request["id"] / "proposal.json"
        self.assertEqual(core.read_json(file)["replacement"], REPLACEMENT)

    def test_proposals_cannot_weaken_validation_or_modify_module_execution(self):
        request = self.request()
        candidates = [REPLACEMENT.replace('raise ValueError("Empty discovery")', 'return []'),
                      REPLACEMENT + '\nprint("untrusted module side effect")\n',
                      REPLACEMENT.replace('import json', 'import os'),
                      REPLACEMENT.replace('def extract_emails(page_html):',
                                          'def extract_emails(page_html=open("secret")):')]
        for candidate in candidates:
            with self.assertRaisesRegex(core.RefreshError, "protected code"):
                repair.record_proposal(self.stage, request["id"], candidate, "An unsafe parser change proposal")
        self.assertEqual(self.adapter.read_text(encoding="utf-8"), SOURCE)

    def test_bad_python_noop_and_stale_source_are_refused(self):
        request = self.request()
        for candidate in ("not python: !!!", SOURCE, SOURCE + "\n# only a comment\n"):
            with self.assertRaises(core.RefreshError):
                repair.record_proposal(self.stage, request["id"], candidate, "This should not become a proposal")
        self.adapter.write_text(SOURCE + "\n# actual source changed\n", encoding="utf-8")
        with self.assertRaisesRegex(core.RefreshError, "Source changed"):
            repair.record_proposal(self.stage, request["id"], REPLACEMENT, "Parser update on now stale source")

    def test_second_proposal_does_not_overwrite_first(self):
        request = self.request()
        repair.record_proposal(self.stage, request["id"], REPLACEMENT, "First parser repair for review")
        file = self.stage / "data/refresh/repair_requests" / request["id"] / "proposal.json"
        before = file.read_bytes()
        with self.assertRaises(core.RefreshError):
            repair.record_proposal(self.stage, request["id"], REPLACEMENT, "Second parser repair for review")
        self.assertEqual(file.read_bytes(), before)

    def test_manual_identity_review_cannot_be_used_to_propose_parser_patch(self):
        self.report["issues"] = [{"code": "EMAIL_PERSON_ASSOCIATION_CHANGED", "count": 1}]
        request = self.request()
        with self.assertRaisesRegex(core.RefreshError, "manual review"):
            repair.record_proposal(self.stage, request["id"], REPLACEMENT, "Do not patch matching to pass a gate")

    def test_unknown_incident_and_path_traversal_are_refused(self):
        for identity in ("../escape", "c" * 64):
            with self.assertRaises(core.RefreshError):
                repair.record_proposal(self.stage, identity, REPLACEMENT, "Unknown incident must be rejected")

    def test_early_failure_incident_is_safe_manual_and_idempotent(self):
        args = (self.stage, "sec_firms", "SOURCE_COMMAND_FAILED", "a" * 64, "b" * 64)
        first = repair.queue_acquisition_failure(*args)
        file = self.stage / "data/refresh/acquisition_incidents" / first["id"] / "incident.json"
        before = file.read_bytes()
        self.assertEqual(repair.queue_acquisition_failure(*args), first)
        self.assertEqual(file.read_bytes(), before)
        self.assertEqual(first["status"], "manual_review")
        self.assertFalse(first["modelAuthorized"])
        self.assertEqual(set(first["diagnostic"]), {"sourceId", "code", "baselineSha256", "commandSha256"})
        with self.assertRaises(core.RefreshError):
            repair.queue_acquisition_failure(self.stage, "act", "SECRET=token", "a" * 64, "b" * 64)
        with self.assertRaises(core.RefreshError):
            repair.queue_acquisition_failure(self.stage, "../escape", "SOURCE_COMMAND_FAILED", "a" * 64, "b" * 64)


if __name__ == "__main__":
    unittest.main()
