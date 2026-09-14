from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from morgan_team_app_coverage import classify


class MorganTeamAppCoverageTests(unittest.TestCase):
    def test_exact_email_precedes_name_team(self):
        team = [{"Name": "Chris Example", "Team Name": "Alpha", "Email": "chris@ms.com"}]
        advisors = {
            "1": {"fc": "149777", "n": "Christopher Example", "tn": "Beta",
                  "e": "chris@ms.com", "t": "high"}}
        rows, summary = classify(team, advisors)
        self.assertEqual("exact_email", rows[0]["app_match_method"])
        self.assertEqual("1", rows[0]["app_crd"])
        self.assertEqual(1, summary["presentByExactEmail"])

    def test_unique_exact_name_and_team_is_a_secondary_link(self):
        team = [{"Name": "Alex Example", "Team Name": "Alpha", "Email": ""}]
        advisors = {
            "2": {"fc": "149777", "n": "Alex Example", "tn": "Alpha", "e": ""}}
        rows, _ = classify(team, advisors)
        self.assertEqual("unique_exact_name_team", rows[0]["app_match_method"])
        self.assertEqual("2", rows[0]["app_crd"])

    def test_ambiguous_email_is_not_linked(self):
        team = [{"Name": "Pat Person", "Team Name": "Alpha", "Email": "shared@ms.com"}]
        advisors = {
            "3": {"fc": "149777", "n": "Pat Person", "tn": "Beta", "e": "shared@ms.com"},
            "4": {"fc": "149777", "n": "Other Person", "tn": "Gamma", "e": "shared@ms.com"},
        }
        rows, summary = classify(team, advisors)
        self.assertEqual("ambiguous_email", rows[0]["app_match_method"])
        self.assertEqual(
            "published_email_links_to_multiple_app_crds",
            rows[0]["unresolved_reason"])
        self.assertEqual(1, summary["ambiguousEmail"])
        self.assertEqual(1, summary["unresolvedWithPublishedEmail"])

    def test_ambiguous_email_never_falls_back_to_name_and_team(self):
        team = [{"Name": "Pat Person", "Team Name": "Alpha",
                 "Email": "shared@ms.com"}]
        advisors = {
            "3": {"fc": "149777", "n": "Pat Person", "tn": "Alpha",
                  "e": "shared@ms.com"},
            "4": {"fc": "149777", "n": "Other Person", "tn": "Gamma",
                  "e": "shared@ms.com"},
        }
        rows, _ = classify(team, advisors)
        self.assertEqual("", rows[0]["app_crd"])
        self.assertEqual("ambiguous_email", rows[0]["app_match_method"])

    def test_other_firm_contacts_cannot_match(self):
        team = [{"Name": "Alex Example", "Team Name": "Alpha", "Email": "alex@example.com"}]
        advisors = {
            "5": {"fc": "999", "n": "Alex Example", "tn": "Alpha", "e": "alex@example.com"}}
        rows, _ = classify(team, advisors)
        self.assertEqual("unresolved", rows[0]["app_match_method"])


if __name__ == "__main__":
    unittest.main()
