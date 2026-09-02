from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from act_economic_links import (account_bucket, approved_link_map,
                                choose_with_margin,
                                enforce_global_one_to_one, firm_agreement,
                                paired_location, safety_fields,
                                sec_name_agreement, spreadsheet_safe,
                                unique_validated_roster_email)
from build_act_assets import load_deployed_advisor_crds, totals_for
from build_act_economic_links import (validate_act_ids, validate_domain_policy,
                                      validate_pinned_inputs)
from contact_provenance import sha256_file
from identity_schema import content_hash


class ActEconomicLinksTest(unittest.TestCase):
    def test_roster_email_must_be_personal_unique_and_validated(self):
        rows = [
            {"email": "one@example.com", "advisor_crd": "123", "tier": "high"},
            {"email": "same@example.com", "advisor_crd": "124", "tier": "high",
             "source_file": "same.csv", "name": "Same Person"},
            {"email": "same@example.com", "advisor_crd": "124", "tier": "high",
             "source_file": "same.csv", "name": "Same Person"},
            {"email": "collision@example.com", "advisor_crd": "125",
             "tier": "high", "source_file": "a.csv"},
            {"email": "collision@example.com", "advisor_crd": "125",
             "tier": "review", "source_file": "b.csv"},
            {"email": "competing@example.com", "advisor_crd": "126",
             "tier": "high"},
            {"email": "competing@example.com", "advisor_crd": "127",
             "tier": "high"},
            {"email": "info@example.com", "advisor_crd": "128", "tier": "high"},
        ]
        self.assertEqual({"one@example.com", "same@example.com"},
                         set(unique_validated_roster_email(rows)))

    def test_sec_name_uses_filed_alias_and_suffix_veto(self):
        record = {"norm_first_name": "Chris", "norm_last_name": "Tolman",
                  "raw_suffix": ""}
        sec = {"first_name": "Christopher", "last_name": "Tolman", "suffix": ""}
        self.assertTrue(sec_name_agreement(record, sec, [])[0])
        sec["suffix"] = "Jr"
        self.assertTrue(sec_name_agreement(record, sec, [])[0])
        self.assertFalse(sec_name_agreement(
            record, sec, [], require_suffix_presence_match=True)[0])
        record["raw_suffix"] = "III"
        sec["suffix"] = "II"
        self.assertFalse(sec_name_agreement(record, sec, [])[0])

    def test_location_is_paired_not_independent(self):
        rec = {"raw_city": "Atlanta", "raw_state": "GA", "raw_postal": ""}
        split = [{"city": "Atlanta", "state": "FL"},
                 {"city": "Miami", "state": "GA"}]
        self.assertFalse(paired_location(rec, split)[0])
        self.assertTrue(paired_location(
            rec, [{"city": "Atlanta", "state": "GA"}])[0])

    def test_firm_family_or_exact_current_company_required(self):
        current = {"firm_crds": {"100"}, "firm_names": {"Current Firm LLC"}}
        self.assertTrue(firm_agreement({}, current, ["100"])[0])
        self.assertTrue(firm_agreement(
            {"raw_company": "Current Firm LLC"}, current, [])[0])
        self.assertFalse(firm_agreement(
            {"raw_company": "Former Firm"}, current, ["200"])[0])
        accepted, reason = firm_agreement(
            {"raw_company": "Current Firm LLC"}, current, ["200"])
        self.assertFalse(accepted)
        self.assertEqual("authoritative_firm_family_mismatch", reason)

    def test_margin_and_global_collision_fail_closed(self):
        winner = choose_with_margin([(1.0, "100"), (.7, "200")])
        tied = choose_with_margin([(1.0, "100"), (.95, "200")])
        self.assertEqual("winner", winner[3])
        self.assertEqual("ambiguous_runner_up", tied[3])
        proposals = {"a": winner, "b": (winner[0], .9, .9, "winner")}
        blocked = enforce_global_one_to_one(proposals)
        self.assertEqual({"global_crd_collision"},
                         {blocked["a"][3], blocked["b"][3]})

    def test_asset_link_has_no_contact_authority(self):
        self.assertEqual({
            "can_email": False, "can_call": False, "can_sync_act": False,
            "can_supply_name": False}, safety_fields())
        self.assertEqual("identity_approved", account_bucket(
            ["residual_strict", "identity_approved"], ["approved"]))
        self.assertEqual("review", account_bucket([], ["unmatched", "review"]))

    def test_only_approved_links_publish(self):
        rows = [
            {"act_id": "a", "advisor_crd": "100",
             "economic_status": "approved", "link_type": "identity_approved"},
            {"act_id": "b", "advisor_crd": "200",
             "economic_status": "review", "link_type": ""},
        ]
        self.assertEqual({"a": ("100", "confirmed")},
                         approved_link_map(rows))
        rows[1]["can_email"] = True
        with self.assertRaises(ValueError):
            approved_link_map(rows)
        duplicate = [
            {"act_id": "a", "advisor_crd": "100",
             "economic_status": "approved", "link_type": "roster_exact"},
            {"act_id": "b", "advisor_crd": "100",
             "economic_status": "approved", "link_type": "residual_strict"},
        ]
        with self.assertRaises(ValueError):
            approved_link_map(duplicate)
        unknown = [{"act_id": "x", "advisor_crd": "300",
                    "economic_status": "approved", "link_type": "manual"}]
        with self.assertRaises(ValueError):
            approved_link_map(unknown)
        with self.assertRaises(ValueError):
            approved_link_map([{"act_id": "", "economic_status": "review"}])
        with self.assertRaises(ValueError):
            approved_link_map([{"act_id": "x", "economic_status": "review"},
                               {"act_id": "x", "economic_status": "review"}])

    def test_report_values_are_formula_escaped(self):
        self.assertEqual("'=HYPERLINK(\"bad\")",
                         spreadsheet_safe("=HYPERLINK(\"bad\")"))
        self.assertEqual("'  @bad", spreadsheet_safe("  @bad"))
        self.assertEqual("Normal", spreadsheet_safe("Normal"))

    def test_live_sec_and_rosters_must_equal_identity_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            interim = root / "interim"
            rosters = root / "raw" / "firm_rosters"
            interim.mkdir()
            rosters.mkdir(parents=True)
            sec_names = {
                "advisors.parquet", "advisor_branches.parquet",
                "advisor_employments.parquet",
                "advisor_employment_history.parquet",
                "advisor_other_names.parquet",
            }
            for name in sec_names:
                (interim / name).write_bytes(name.encode())
            roster = rosters / "firm.csv"
            roster.write_text("name,email\nA,a@example.com\n", encoding="utf-8")
            manifest = {
                "rosterEvidence": True,
                "secSources": {
                    name: sha256_file(interim / name) for name in sec_names},
                "rosterSources": {roster.name: sha256_file(roster)},
            }
            with mock.patch("build_act_economic_links.INTERIM", interim), \
                    mock.patch("build_act_economic_links.RAW", root / "raw"):
                validate_pinned_inputs(manifest)
                roster.write_text(
                    "name,email\nB,b@example.com\n", encoding="utf-8")
                with self.assertRaises(SystemExit):
                    validate_pinned_inputs(manifest)

    def test_domain_policy_must_equal_identity_manifest(self):
        policy = {"example.com": {"status": "authoritative",
                                  "allowedFirmCrds": ["123"]}}
        manifest = {"rosterDomainPolicyHash": content_hash(policy)}
        self.assertEqual(content_hash(policy),
                         validate_domain_policy(manifest, policy))
        with self.assertRaises(SystemExit):
            validate_domain_policy(manifest, {**policy, "other.com": {}})

    def test_act_ids_fail_before_dictionary_collapse(self):
        validate_act_ids([{"id": "a"}, {"id": "b"}])
        with self.assertRaises(SystemExit):
            validate_act_ids([{"id": ""}])
        with self.assertRaises(SystemExit):
            validate_act_ids([{"id": "a"}, {"id": "a"}])

    def test_deployed_index_and_four_product_totals(self):
        accounts = {
            "one": {"acv_sma": 1, "large": 2, "fund": 3, "midcap": 4}}
        self.assertEqual(
            {"acv": 1, "lcv": 2, "mf": 3, "midcap": 4, "accounts": 1},
            totals_for(accounts, {"one"}))
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "advisor_index.json"
            path.write_text(json.dumps({"advisors": [
                ["100", "One"], ["200", "Two"]]}), encoding="utf-8")
            with mock.patch("build_act_assets.ADVISOR_INDEX", path):
                crds, fact = load_deployed_advisor_crds()
                self.assertEqual({"100", "200"}, crds)
                self.assertEqual(2, fact["rows"])
                self.assertEqual(sha256_file(path), fact["sha256"])
                path.write_text(json.dumps({"advisors": [
                    ["100", "One"], ["100", "Again"]]}), encoding="utf-8")
                with self.assertRaises(SystemExit):
                    load_deployed_advisor_crds()


if __name__ == "__main__":
    unittest.main()
