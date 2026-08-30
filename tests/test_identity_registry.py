from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from export_approved_recipients import (
    RELEASE_PROVENANCE_KEYS, bounded_match_score, build_registry,
    build_release_descriptor, registry_quality_summary,
)
from export_act_crd_corrections import REQUIRED_ACT_IDS
import build_act_lookup
from identity_schema import LINKS_FILENAME, MANIFEST_FILENAME, content_hash, runtime_content_hash
from import_act_identity_decisions import normalize_decision, validate


def link(crd, email, act_id, status="approved"):
    return {"advisor_crd": crd, "claimed_crd": crd,
            "identity_status": status, "decision_reason": "test",
            "can_email": status == "approved", "email": email,
            "display_name": f"Person {crd}", "legal_name": f"Person {crd}",
            "email_greeting": "Person", "act_last_name": str(crd),
            "firm": "Firm", "source_record_id": act_id}


class RegistryTests(unittest.TestCase):
    def test_release_descriptor_is_deterministic_and_pii_free(self):
        provenance = {key: f"value-{key}"
                      for key in RELEASE_PROVENANCE_KEYS}
        payload = {"schemaVersion": 1, "contentHash": "a" * 64,
                   "recipients": {"100": {}}, "ineligible": {"200": "x"},
                   "provenance": provenance}
        first = build_release_descriptor(payload)
        self.assertEqual(first, build_release_descriptor(payload))
        self.assertEqual(set(RELEASE_PROVENANCE_KEYS),
                         set(first["provenance"]))
        self.assertNotIn("recipients", first)
        self.assertNotIn("ineligible", first)
        self.assertEqual(content_hash({k: v for k, v in first.items()
                                       if k != "descriptorHash"}),
                         first["descriptorHash"])
        payload["provenance"]["recipientEmail"] = "person@example.com"
        with self.assertRaisesRegex(ValueError, "unapproved fields"):
            build_release_descriptor(payload)

    def test_required_correction_report_sentinel_is_pinned_by_default(self):
        self.assertIn("855992fd-e032-4454-947b-3dcd5515dfe3",
                      REQUIRED_ACT_IDS)

    def test_act_lookup_uses_only_unique_approved_ledger_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            links_path = root / LINKS_FILENAME
            pd.DataFrame([
                {"advisor_crd": "100", "source_record_id": "a1",
                 "identity_status": "approved", "can_sync_act": True},
                {"advisor_crd": "200", "source_record_id": "a2",
                 "identity_status": "approved", "can_sync_act": True},
                {"advisor_crd": "200", "source_record_id": "a3",
                 "identity_status": "approved", "can_sync_act": True},
                {"advisor_crd": "400", "source_record_id": "same",
                 "identity_status": "approved", "can_sync_act": True},
                {"advisor_crd": "500", "source_record_id": "same",
                 "identity_status": "approved", "can_sync_act": True},
                {"advisor_crd": "600", "source_record_id": "a6",
                 "identity_status": "quarantine", "can_sync_act": False},
            ]).to_parquet(links_path, index=False)
            digest = hashlib.sha256(links_path.read_bytes()).hexdigest()
            core = {"outputs": {LINKS_FILENAME: {
                "sha256": digest, "rows": 6}}}
            manifest = {**core, "generatedUtc": "2026-08-26T00:00:00Z",
                        "contentHash": content_hash(core)}
            (root / MANIFEST_FILENAME).write_text(
                json.dumps(manifest), encoding="utf-8")
            with mock.patch.object(build_act_lookup, "IDENTITY", root):
                mapping, _ = build_act_lookup.approved_pairs()
            self.assertEqual({"100": "a1"}, mapping)

    def test_registry_content_and_routing_hash_contract(self):
        payload = build_registry(pd.DataFrame([
            link("1", "one@example.com", "act-1")]))
        core = {k: payload[k] for k in
                ("schemaVersion", "recipients", "ineligible", "provenance")}
        self.assertEqual(runtime_content_hash(core), payload["contentHash"])
        recipient = payload["recipients"]["1"]
        route = {"crd": "1", "email": "one@example.com",
                 "actContactId": "act-1", "teammates": []}
        self.assertEqual(content_hash(route), recipient["routingHash"])

    def test_duplicate_email_is_not_resolved(self):
        payload = build_registry(pd.DataFrame([
            link("1", "shared@example.com", "a1"),
            link("2", "shared@example.com", "a2")]))
        self.assertEqual({}, payload["recipients"])
        self.assertEqual("approved_email_not_unique",
                         payload["ineligible"]["1"])

    def test_confirmed_and_high_are_authorized_but_review_is_blocked(self):
        contacts = {"advisors": {
            "900": {"e": "roster@example.com", "n": "Roster Person",
                    "cn": "Roster Firm", "src": "UBS", "t": "high",
                    "ms": 0.82},
            "901": {"e": "direct@example.com", "n": "Direct Person",
                    "cn": "Roster Firm", "src": "Cetera",
                    "t": "confirmed", "ms": 1.0},
            "902": {"e": "review@example.com", "n": "Review Person",
                    "cn": "Roster Firm", "src": "UBS", "t": "review",
                    "ms": 0.90},
            "903": {"e": "unscored@example.com", "n": "Unscored Person",
                    "cn": "Roster Firm", "src": "UBS", "t": "high"},
        }, "teams": {}, "practices": {}}
        payload = build_registry(pd.DataFrame(columns=[
            "advisor_crd", "identity_status", "can_email", "email"]),
            contacts)
        self.assertEqual({"900", "901"}, set(payload["recipients"]))
        self.assertEqual("high", payload["recipients"]["900"]["tier"])
        self.assertEqual(0.82, payload["recipients"]["900"]["matchScore"])
        self.assertEqual("confirmed", payload["recipients"]["901"]["tier"])
        self.assertNotIn("902", payload["recipients"])
        self.assertEqual("contact_identity_not_approved",
                         payload["ineligible"]["902"])
        self.assertEqual("high_match_evidence_missing",
                         payload["ineligible"]["903"])

    def test_match_score_is_bounded_model_evidence_not_probability(self):
        self.assertEqual(0.72, bounded_match_score("0.72"))
        self.assertEqual(1.18, bounded_match_score(1.18))
        for value in (-0.01, 1.181, float("inf"), "not-a-score", None):
            self.assertIsNone(bounded_match_score(value))

    def test_registry_quality_summary_counts_tiers_sources_and_score_coverage(self):
        payload = {"recipients": {
            "1": {"tier": "confirmed", "source": "CRM", "matchScore": 1},
            "2": {"tier": "high", "source": "UBS", "matchScore": 0.9},
            "3": {"tier": "high", "source": "UBS"},
        }, "ineligible": {"4": "contact_identity_not_approved"}}
        summary = registry_quality_summary(payload)
        self.assertEqual({"confirmed": 1, "high": 2}, summary["tiers"])
        self.assertEqual({"CRM": 1, "UBS": 2}, summary["sources"])
        self.assertIn({"tier": "high", "source": "UBS", "recipients": 2,
                       "withMatchScore": 1}, summary["tierSources"])
        self.assertEqual({"contact_identity_not_approved": 1},
                         summary["ineligibleReasons"])

    def test_direct_crd_roster_contact_remains_eligible(self):
        contacts = {"advisors": {"901": {
            "e": "direct@example.com", "n": "Direct Person",
            "cn": "Roster Firm", "src": "Cetera", "t": "confirmed",
        }}, "teams": {}, "practices": {}}
        payload = build_registry(pd.DataFrame(columns=[
            "advisor_crd", "identity_status", "can_email", "email"]),
            contacts)
        self.assertEqual("direct@example.com",
                         payload["recipients"]["901"]["email"])
        self.assertEqual("", payload["recipients"]["901"]["actContactId"])

    def test_roster_route_requires_current_sec_firm_family_in_production(self):
        contacts = {"advisors": {"901": {
            "e": "person@ubs.com", "n": "Person", "cn": "UBS",
            "src": "UBS", "t": "high", "ms": 0.9, "rf": ["8174"],
        }}, "teams": {}, "practices": {}}
        links = pd.DataFrame(columns=[
            "advisor_crd", "identity_status", "can_email", "email"])
        blocked = build_registry(links, contacts, current_firms={
            "901": {"999"}})
        self.assertNotIn("901", blocked["recipients"])
        self.assertEqual("roster_current_firm_conflict",
                         blocked["ineligible"]["901"])
        allowed = build_registry(links, contacts, current_firms={
            "901": {"8174"}})
        self.assertIn("901", allowed["recipients"])

    def test_crm_contact_requires_exact_approved_ledger_email(self):
        contacts = {"advisors": {"123": {
            "e": "changed@example.com", "n": "Act Person",
            "cn": "Firm", "src": "CRM", "t": "confirmed",
        }}, "teams": {}, "practices": {}}
        payload = build_registry(pd.DataFrame([
            link("123", "approved@example.com", "act-1")]), contacts)
        self.assertNotIn("123", payload["recipients"])
        self.assertEqual("act_email_does_not_match_approved_identity",
                         payload["ineligible"]["123"])

    def test_decision_validation_requires_current_hash_and_reviewer(self):
        row = {"act_id": "a1", "expected_evidence_hash": "hash",
               "link_decision": "approve", "resolved_crd": "123",
               "reviewer": "Colleague", "reason_code": "verified_iapd"}
        decision = normalize_decision(row, "2026-08-26T00:00:00Z")
        self.assertEqual([], validate(decision, {"a1": "hash"}, {"123"}))
        decision["expected_evidence_hash"] = "stale"
        self.assertIn("stale_evidence_hash",
                      validate(decision, {"a1": "hash"}, {"123"}))

    def test_production_identity_pipeline_has_no_excel_input(self):
        for name in ("identity_resolver.py", "build_identity_ledger.py",
                     "export_approved_recipients.py"):
            source = (ROOT / "src" / name).read_text(encoding="utf-8").lower()
            self.assertNotIn("read_excel", source)
            self.assertNotIn("crm_contacts_", source)
        import act_crosswalk
        import build_contacts
        import inspect
        self.assertNotIn("read_excel", inspect.getsource(build_contacts.load_crm))
        self.assertNotIn("read_excel", inspect.getsource(act_crosswalk.main))
        self.assertNotIn("act_crosswalk", (ROOT / "src" / "build_act_lookup.py")
                         .read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
