"""Corroborated SEC-name emission; existing selection safeguards stay intact."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import reconcile_display_names as names


class CorroboratedDisplayNameTests(unittest.TestCase):
    def decide(self, advisor=None, contact=None):
        row = {"advisor_crd": "1", "first_name": "CHARLES", "middle_name": "RYAN",
               "last_name": "HOLT", "used_first_name": "CHRISTOPHER"}
        row.update(advisor or {})
        entry = {"n": "Ryan Holt", "t": "high", "e": "ryan.holt@example.invalid",
                 "pu": "https://example.invalid/ryan-holt"}
        entry.update(contact or {})
        with tempfile.TemporaryDirectory() as directory:
            web = Path(directory)
            (web / "contacts.json").write_text(json.dumps({"advisors": {"1": entry}}), encoding="utf-8")
            with mock.patch.object(names, "WEB", web), mock.patch.object(names.pd, "read_parquet", return_value=pd.DataFrame([row])):
                return names.decide()

    def test_initial_filed_first_does_not_replace_corroborated_middle(self):
        chosen, _ = self.decide({"first_name": "C", "middle_name": "BROOKS",
            "last_name": "WOODRUFF", "used_first_name": "COMER"},
            {"n": "Brooks Woodruff", "e": "brooks.woodruff@example.invalid", "pu": ""})
        self.assertEqual(chosen, {"1": ("Brooks Woodruff", "Comer Woodruff")})

    def test_noninitial_unrelated_first_does_not_replace_corroborated_middle(self):
        chosen, _ = self.decide()
        self.assertEqual(chosen, {"1": ("Ryan Holt", "Christopher Holt")})

    def test_known_first_name_route_is_preserved(self):
        chosen, _ = self.decide({"first_name": "RAJ", "middle_name": "",
            "last_name": "SHARMA", "used_first_name": "NAGANATH"},
            {"n": "Raj Sharma", "e": "raj.sharma@example.invalid", "pu": ""})
        self.assertEqual(chosen, {"1": ("Raj Sharma", "Naganath Sharma")})

    def test_shortening_route_still_requires_two_independent_signals(self):
        advisor = {"first_name": "THOMAS", "middle_name": "", "used_first_name": "TIMONTHY"}
        contact = {"n": "Tim Holt", "e": "tim.holt@example.invalid", "pu": "https://example.invalid/tim-holt"}
        chosen, _ = self.decide(advisor, contact)
        self.assertEqual(chosen, {"1": ("Tim Holt", "Timonthy Holt")})
        contact["pu"] = ""
        chosen, skipped = self.decide(advisor, contact)
        self.assertEqual(chosen, {})
        self.assertEqual(skipped["no_corroboration"], 1)

    def test_bare_initial_candidate_remains_excluded(self):
        chosen, skipped = self.decide(contact={"n": "R Holt", "e": "r.holt@example.invalid"})
        self.assertEqual(chosen, {})
        self.assertEqual(skipped["initial"], 1)

    def test_known_middle_requires_independent_corroboration(self):
        chosen, skipped = self.decide(contact={"e": "unrelated@example.invalid", "pu": "https://example.invalid/other"})
        self.assertEqual(chosen, {})
        self.assertEqual(skipped["no_corroboration"], 1)

    def test_review_tier_and_wrong_surname_remain_excluded(self):
        for contact, reason in [({"t": "review"}, "tier"), ({"n": "Ryan Wrong"}, "surname")]:
            chosen, skipped = self.decide(contact=contact)
            self.assertEqual(chosen, {})
            self.assertEqual(skipped[reason], 1)

    def test_unknown_name_cannot_be_introduced_by_two_signals_alone(self):
        chosen, skipped = self.decide(contact={"n": "Zack Holt", "e": "zack.holt@example.invalid",
                                             "pu": "https://example.invalid/zack-holt"})
        self.assertEqual(chosen, {})
        self.assertEqual(skipped["not_in_sec"], 1)

    def test_existing_used_nickname_is_not_overwritten(self):
        chosen, _ = self.decide({"first_name": "WILLIAM", "used_first_name": "BILL", "middle_name": ""},
                               {"n": "William Holt", "e": "william.holt@example.invalid"})
        self.assertEqual(chosen, {})
        chosen, skipped = self.decide(contact={"n": "Christopher Holt"})
        self.assertEqual(chosen, {})
        self.assertEqual(skipped["not_in_sec"], 1)


if __name__ == "__main__":
    unittest.main()
