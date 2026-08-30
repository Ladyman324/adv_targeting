from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from contact_identity_compare import (compare_contacts, compare_registries,
                                      relationship, review_added_routes,
                                      review_email_moves,
                                      sec_published_name_exact)


class ComparisonTests(unittest.TestCase):
    def row(self, crd, email="a@example.com", source="UBS"):
        return {"key": "email:" + email, "crd": crd, "email": email,
                "name": "A Person", "phone": "", "source": source,
                "tier": "high", "score": .9, "source_firm": "8174",
                "roster_family": "8174"}

    def test_email_stably_exposes_crd_change(self):
        diff, summary = compare_contacts([self.row("1")], [self.row("2")],
                                         {"1": {"8174"}}, {"2": {"8174"}})
        self.assertEqual("crd_changed", diff.iloc[0]["change"])
        self.assertEqual(1, summary["changes"]["crd_changed"])

    def test_firm_family_disagreement_is_visible(self):
        self.assertEqual("cross_family", relationship(
            self.row("1"), {"1": {"999"}}))

    def test_registry_reports_same_email_moving_crd(self):
        before = {"recipients": {"1": {"email": "a@example.com"}}}
        after = {"recipients": {"2": {"email": "a@example.com"}}}
        _, summary = compare_registries(before, after)
        self.assertEqual(1, summary["changes"]["email_crd_moved"])

    def test_sec_exact_requires_literal_non_initial_given_name(self):
        names = {"1": [{"first_name": "Kathy", "middle_name": "J",
                         "last_name": "Randall"}]}
        self.assertFalse(sec_published_name_exact(
            {**self.row("1"), "name": "J.R. Randall"}, "1", names))

    def test_move_is_explained_by_stronger_person_specific_sec_name(self):
        old = {**self.row("1"), "name": "Nicole Tesoriero"}
        new = {**self.row("2"), "name": "Nicole Tesoriero"}
        names = {
            "1": [{"first_name": "Kristen", "last_name": "Tesoriero"}],
            "2": [{"first_name": "Nicole", "last_name": "Tesoriero"}],
        }
        moves = [{"event": "email_crd_moved", "email": "a@example.com",
                  "baseline_crds": "1", "candidate_crds": "2"}]
        reviewed = review_email_moves(
            moves, [old], [new], {"1": {"8174"}}, {"2": {"8174"}}, names)
        self.assertTrue(reviewed[0]["explained"])

    def test_new_roster_route_requires_score_unique_email_and_current_family(self):
        before = {"recipients": {}}
        after = {"recipients": {"2": {
            "email": "a@example.com", "name": "A Person", "source": "UBS",
            "tier": "high", "matchScore": 0.9}}}
        detail, summary = review_added_routes(
            before, after, [self.row("2")], {"2": {"8174"}})
        self.assertEqual(1, summary["safe"])
        self.assertTrue(detail.iloc[0]["safe"])

    def test_new_cross_family_roster_route_is_unsafe(self):
        before = {"recipients": {}}
        after = {"recipients": {"2": {
            "email": "a@example.com", "name": "A Person", "source": "UBS",
            "tier": "high", "matchScore": 0.9}}}
        detail, summary = review_added_routes(
            before, after, [self.row("2")], {"2": {"999"}})
        self.assertEqual(1, summary["unsafe"])
        self.assertIn("roster_current_firm_disagrees", detail.iloc[0]["issues"])


if __name__ == "__main__":
    unittest.main()
