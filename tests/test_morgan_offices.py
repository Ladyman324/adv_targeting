from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from morgan_office_async import PROFILE_TYPES, association_team_record, params


class MorganOfficeTests(unittest.TestCase):
    def test_profiles_are_non_person_entities_only(self):
        self.assertEqual(("Branch", "Complex"), PROFILE_TYPES)

    def test_pagination_reuses_the_supplied_search_session(self):
        first = params("Branch", 0, "fixed-session")
        later = params("Branch", 50, "fixed-session")
        self.assertEqual("fixed-session", first["session_id"])
        self.assertEqual(first["session_id"], later["session_id"])
        self.assertEqual("50", later["offset"])
        filters = json.loads(later["filters"])
        self.assertEqual("Branch", filters["c_profileType"]["$eq"])

    def test_office_capture_does_not_write_to_contact_roster_folder(self):
        source = (ROOT / "src" / "morgan_office_async.py").read_text(
            encoding="utf-8")
        self.assertIn('scratch_path("morgan_stanley", "offices"', source)
        self.assertIn('scratch_path("morgan_stanley", "teams"', source)
        self.assertNotIn('roster_path("morgan_stanley"', source)

    def test_branch_team_association_becomes_a_seed_and_email_audit(self):
        row = association_team_record({
            "c_profileType": "Team", "c_pagesName": "The Marinelli Group",
            "c_pagesURL": "https://advisor.morganstanley.com/the-marinelli-group",
            "emails": ["Courtney.Shane@morganstanley.com",
                       "Domenic.Marinelli@morganstanley.com"],
            "address": {"city": "Boston", "region": "MA"},
        }, {"c_branchName": "Boston", "c_officeNumber": "123"})
        self.assertEqual("The Marinelli Group", row["Team Name"])
        self.assertEqual(2, row["Published Email Count"])
        self.assertIn("courtney.shane@morganstanley.com",
                      row["Published Team Emails"])
        self.assertEqual("Boston", row["City"])

    def test_non_team_association_is_not_a_team_seed(self):
        self.assertIsNone(association_team_record(
            {"c_profileType": "FA", "c_pagesURL":
             "https://advisor.morganstanley.com/person"}, {}))


if __name__ == "__main__":
    unittest.main()
