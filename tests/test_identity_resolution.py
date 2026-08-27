from __future__ import annotations

import json
import pathlib
import sys
import unittest

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from build_contacts import pick_best, score_contacts
from identity_normalize import (given_agreement, normalize_crd,
                                parse_full_name, preferred_name_status)
from identity_resolver import (apply_decision, evaluate_assertion,
                               prepare_act_records)
from identity_schema import content_hash


def act(act_id="a1", crd="123", first="Chris", last="Tolman",
        salutation="Chris", email="chris@example.com"):
    return {"id": act_id, "fullName": f"Mr. {first} {last}",
            "firstName": first, "lastName": last, "salutation": salutation,
            "emailAddress": email, "businessPhone": "2125551000",
            "company": "Example Firm",
            "businessAddress": {"line1": "1 Main St", "city": "New York",
                                "state": "NY", "postalCode": "10001"},
            "customFields": {"crd": crd}}


def sec(crd="123", first="Christopher", last="Tolman"):
    return {"advisor_crd": crd, "first_name": first, "middle_name": "",
            "used_first_name": "", "last_name": last, "suffix": ""}


class NormalizationTests(unittest.TestCase):
    def test_crd_is_strict_and_name_parser_removes_honorific(self):
        self.assertEqual("123", normalize_crd("00123.0"))
        self.assertEqual("", normalize_crd("CRD 123"))
        parsed = parse_full_name("Mr. Christopher Tolman, CFP")
        self.assertEqual("", parsed.last)  # commas are deliberately review-only
        self.assertEqual("Tolman", parse_full_name("Mr. Christopher Tolman").last)

    def test_chris_is_strict_but_bo_requires_review(self):
        self.assertEqual((True, "given_strict_nickname"),
                         given_agreement(["Chris"], ["Christopher"]))
        self.assertEqual(("approved_auto", "preferred_strict_nickname"),
                         preferred_name_status("Chris", "Christopher"))
        self.assertEqual(("review", "preferred_requires_review"),
                         preferred_name_status("Bo", "Robert"))


class ResolverTests(unittest.TestCase):
    def test_valid_one_to_one_assertion_is_approved(self):
        record = prepare_act_records([act()], "act.json", "abc")[0]
        evidence, link = evaluate_assertion(
            record, sec(), {"current_firm_names": ["examplefirm"]})
        self.assertEqual("approved", link["identity_status"])
        self.assertTrue(link["can_email"])
        self.assertEqual("Chris", link["email_greeting"])
        self.assertEqual([], json.loads(evidence["hard_conflicts_json"]))

    def test_same_name_without_independent_corroboration_requires_review(self):
        record = prepare_act_records([act()], "act.json", "abc")[0]
        _, link = evaluate_assertion(record, sec())
        self.assertEqual("review", link["identity_status"])
        self.assertEqual("independent_corroboration_required",
                         link["decision_reason"])
        self.assertFalse(link["can_email"])

    def test_same_name_wrong_current_firm_requires_review(self):
        record = prepare_act_records([act()], "act.json", "abc")[0]
        _, link = evaluate_assertion(
            record, sec(), {"current_firm_names": ["differentfirm"]})
        self.assertEqual("review", link["identity_status"])
        self.assertEqual("current_firm_conflict_review",
                         link["decision_reason"])

    def test_address_contradiction_without_other_corroboration_requires_review(self):
        record = prepare_act_records([act()], "act.json", "abc")[0]
        _, link = evaluate_assertion(record, sec(), {
            "current_firm_names": [],
            "branches": [{"street": "99 Elsewhere", "city": "Boston",
                          "state": "MA", "postal": "02108"}],
        })
        self.assertEqual("review", link["identity_status"])
        self.assertEqual("current_address_conflict_review",
                         link["decision_reason"])

    def test_duplicate_crd_and_email_are_quarantined(self):
        records = prepare_act_records(
            [act("a1"), act("a2", first="Christopher")], "act.json", "abc")
        for record in records:
            evidence, link = evaluate_assertion(record, sec())
            self.assertEqual("quarantine", link["identity_status"])
            conflicts = json.loads(evidence["hard_conflicts_json"])
            self.assertIn("duplicate_crd_claim", conflicts)
            self.assertIn("duplicate_email_claim", conflicts)

    def test_inactive_and_expanded_generic_addresses_fail_closed(self):
        inactive = act()
        inactive["isActive"] = False
        record = prepare_act_records([inactive], "act.json", "abc")[0]
        _, link = evaluate_assertion(record, sec())
        self.assertEqual("quarantine", link["identity_status"])
        self.assertIn("inactive_act_record", link["hard_conflicts_json"])
        decision = {"expected_evidence_hash": link["assertion_evidence_hash"],
                    "link_decision": "approve", "resolved_crd": "123"}
        self.assertEqual(
            "quarantine",
            apply_decision(link, decision, {"123": sec()})["identity_status"])

        shared = prepare_act_records(
            [act(email="client.service@example.com")], "act.json", "abc")[0]
        _, shared_link = evaluate_assertion(shared, sec())
        self.assertEqual("quarantine", shared_link["identity_status"])
        self.assertIn("generic_email", shared_link["hard_conflicts_json"])

    def test_invalid_unknown_and_name_conflict_fail_closed(self):
        invalid = prepare_act_records([act(crd="CRD 123")], "act.json", "abc")[0]
        _, link = evaluate_assertion(invalid, None)
        self.assertEqual("quarantine", link["identity_status"])
        wrong = prepare_act_records([act(first="Victoria")], "act.json", "abc")[0]
        _, link = evaluate_assertion(wrong, sec())
        self.assertEqual("quarantine", link["identity_status"])
        self.assertIn("given_name_conflict", link["hard_conflicts_json"])

    def test_stale_decision_is_ignored_and_bound_decision_can_approve_bo(self):
        record = prepare_act_records(
            [act(act_id="bo", crd="4996584", first="Robert",
                 last="Ladyman", salutation="Bo")], "act.json", "abc")[0]
        _, link = evaluate_assertion(
            record, sec("4996584", "Robert", "Ladyman"))
        stale = apply_decision(link, {"expected_evidence_hash": "old",
                                     "link_decision": "approve"},
                               {"4996584": sec("4996584", "Robert", "Ladyman")})
        self.assertEqual("automatic_rules", stale["decision_source"])
        decision = {"expected_evidence_hash": link["assertion_evidence_hash"],
                    "link_decision": "approve", "resolved_crd": "4996584",
                    "preferred_decision": "approve", "preferred_first": "Bo",
                    "reviewer": "Reviewer", "reviewed_utc": "2026-08-26T00:00:00Z",
                    "decision_hash": content_hash({"decision": "bo"})}
        resolved = apply_decision(
            link, decision, {"4996584": sec("4996584", "Robert", "Ladyman")})
        self.assertEqual("Bo", resolved["email_greeting"])
        self.assertEqual("approved_reviewed", resolved["preferred_status"])
        self.assertEqual("human_review", resolved["decision_source"])
        self.assertEqual("Reviewer", resolved["reviewed_by"])

    def test_replacement_recomputes_greeting_from_resolved_sec_identity(self):
        record = prepare_act_records(
            [act(crd="111", first="Christopher", last="Tolman",
                 salutation="Chris")], "act.json", "abc")[0]
        _, link = evaluate_assertion(record, sec("111", "Charles", "Wrong"))
        self.assertEqual("Charles", link["email_greeting"])
        decision = {
            "expected_evidence_hash": link["assertion_evidence_hash"],
            "link_decision": "replace", "resolved_crd": "222",
            "reviewer": "Reviewer", "reviewed_utc": "2026-08-26T00:00:00Z",
            "decision_hash": content_hash({"decision": "replace"}),
        }
        resolved_sec = sec("222", "Christopher", "Tolman")
        resolved_sec["used_first_name"] = "Topher"
        resolved = apply_decision(link, decision, {"222": resolved_sec})
        self.assertEqual("222", resolved["advisor_crd"])
        self.assertEqual("Christopher Tolman", resolved["legal_name"])
        self.assertEqual("Chris", resolved["email_greeting"])
        self.assertEqual("approved_auto", resolved["preferred_status"])

    def test_replacement_does_not_retain_old_candidate_greeting(self):
        record = prepare_act_records(
            [act(crd="111", first="Alice", last="Right",
                 salutation="")], "act.json", "abc")[0]
        _, link = evaluate_assertion(record, sec("111", "Bob", "Wrong"))
        self.assertEqual("Bob", link["email_greeting"])
        decision = {
            "expected_evidence_hash": link["assertion_evidence_hash"],
            "link_decision": "replace", "resolved_crd": "222",
            "reviewer": "Reviewer", "reviewed_utc": "2026-08-26T00:00:00Z",
            "decision_hash": content_hash({"decision": "replace-fallback"}),
        }
        resolved = apply_decision(
            link, decision, {"222": sec("222", "Alice", "Right")})
        self.assertEqual("Alice", resolved["email_greeting"])
        self.assertNotEqual("Bob", resolved["email_greeting"])

    def test_crm_rows_cannot_bypass_the_identity_ledger_match(self):
        people = pd.DataFrame([
            {"source": "CRM", "identity_status": "approved",
             "identity_crd": "123", "given_crd": "999"},
            {"source": "CRM", "identity_status": "unmatched",
             "identity_crd": "", "given_crd": "123"},
        ])
        scored = score_contacts(people, {"person": [("123", [], {})]})
        self.assertEqual(["confirmed", "none"], scored["tier"].tolist())
        self.assertEqual(["123", ""], scored["advisor_crd"].tolist())

    def test_review_row_cannot_donate_an_email_to_an_approved_row(self):
        common = {"phone_kind": "", "phone": "", "phone_ext": "",
                  "title": "", "team": "", "profile_url": "",
                  "linkedin": "", "mobile": ""}
        group = pd.DataFrame([
            dict(common, tier="high", match_score=1.0,
                 source="Roster", email=""),
            dict(common, tier="review", match_score=0.99,
                 source="CRM", email="unsafe@example.com"),
        ])
        self.assertEqual("", pick_best(group)["email"])


if __name__ == "__main__":
    unittest.main()
