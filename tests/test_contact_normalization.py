from __future__ import annotations

import dataclasses
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from contact_normalization import (  # noqa: E402
    classify_excel_phone_gap,
    normalize_act_contact,
    parse_full_name,
    production_act_rows,
)


class ActNameNormalizationTests(unittest.TestCase):
    def test_honorific_corrupted_surname_recovers_full_name(self):
        normalized = normalize_act_contact({
            "id": "act-1", "firstName": "Jordan", "lastName": "Mr.",
            "fullName": "Mr. Jordan Avery Morgan Jr.",
        }, "act_contacts_fixture.json")
        self.assertEqual("full_name_recovered", normalized["name_parse_status"])
        self.assertEqual("Jordan", normalized["name_first"])
        self.assertEqual("Avery", normalized["name_middle"])
        self.assertEqual("Morgan", normalized["name_last"])
        self.assertEqual("Jr.", normalized["name_suffix"])
        self.assertEqual("Jordan Avery Morgan Jr.", normalized["name"])

    def test_valid_structured_middle_and_suffix_are_preserved(self):
        normalized = normalize_act_contact({
            "id": "act-2", "firstName": "Taylor", "middleName": "Q",
            "lastName": "Reed", "nameSuffix": "III",
            "fullName": "Dr. Taylor Reed",
        })
        self.assertEqual("structured", normalized["name_parse_status"])
        self.assertEqual("Taylor Q Reed III", normalized["name"])

    def test_last_comma_first_is_parsed_but_single_token_is_not_invented(self):
        parsed = parse_full_name("Reed, Taylor Q III")
        self.assertEqual(("Taylor", "Q", "Reed", "III"),
                         (parsed.first, parsed.middle, parsed.last, parsed.suffix))
        self.assertEqual("", parse_full_name("Taylor").last)
        suffix_display = parse_full_name("Taylor Q Reed, Jr.")
        self.assertEqual(("Taylor", "Q", "Reed", "Jr."),
                         (suffix_display.first, suffix_display.middle,
                          suffix_display.last, suffix_display.suffix))

    def test_act_phone_extension_mobile_and_crd_are_explicit_evidence(self):
        normalized = normalize_act_contact({
            "id": "act-3", "firstName": "Taylor", "lastName": "Reed",
            "businessPhone": "+1 (404) 555-0100", "businessExtension": "42",
            "mobilePhone": "404-555-0199", "customFields": {"crd": "12345"},
        })
        self.assertEqual("4045550100x42", normalized["phone"])
        self.assertEqual("4045550199", normalized["mobile"])
        self.assertEqual("12345", normalized["asserted_crd"])
        self.assertEqual("act_business_phone", normalized["phone_source_kind"])


class ExcelIsolationTests(unittest.TestCase):
    def test_excel_comparison_returns_counts_never_donor_values(self):
        act = [{"id": "1", "emailAddress": "person@example.com",
                "businessPhone": ""}]
        excel = [{"E-mail": "person@example.com", "Phone": "404-555-0100"}]
        gap = classify_excel_phone_gap(act, excel)
        self.assertEqual(1, gap.uniquely_explained_missing_rows)
        self.assertNotIn("phone", {field.name for field in dataclasses.fields(gap)})

    def test_production_rows_only_emit_act_api_phone_provenance(self):
        rows = production_act_rows([{
            "id": "1", "firstName": "Taylor", "lastName": "Reed",
            "businessPhone": "404-555-0100", "Phone": "999-999-9999",
        }], "act_contacts_fixture.json")
        self.assertEqual("4045550100", rows[0]["phone"])
        self.assertEqual("act_business_phone", rows[0]["phone_source_kind"])
        self.assertEqual("act_contacts_fixture.json", rows[0]["phone_source_file"])


if __name__ == "__main__":
    unittest.main()
