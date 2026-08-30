from __future__ import annotations

import pathlib
import sys
import unittest

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from build_contacts import score_contacts
from forbes_match import name_score


def person(name="Alice Smith", firm="10"):
    return pd.DataFrame([{
        "source": "Test roster", "given_crd": "123", "name": name,
        "email": "alice@example.com", "firm_crd": firm,
        "city": "Atlanta", "state": "GA",
    }])


def index():
    return {"smith": [("123", {"alice", "ally"}, {
        "firms": {"10"}, "cities": {"atlanta"}, "states": {"GA"},
        "suffix": "",
    })]}


class DirectRosterCrdTests(unittest.TestCase):
    def test_equal_bare_initial_is_not_a_full_given_name_match(self):
        self.assertEqual(0.45, name_score(["j", "r"], {"kathy", "jean", "j"}))

    def test_personal_email_disambiguates_display_initials(self):
        frame = person("J.R. Randall")
        frame["given_crd"] = ""
        frame["email"] = "john.p.randall@example.com"
        frame["allowed_firm_crds"] = "10"
        shared = {"firms": {"10"}, "cities": {"atlanta"},
                  "states": {"GA"}, "suffix": ""}
        candidates = {"randall": [
            ("510", {"john", "p"}, shared),
            ("225", {"kathy", "jean", "j"}, shared),
        ]}
        row = score_contacts(frame, candidates).iloc[0]
        self.assertEqual("high", row["tier"])
        self.assertEqual("510", row["advisor_crd"])

    def test_stated_crd_requires_matching_sec_name_and_firm(self):
        row = score_contacts(person(), index()).iloc[0]
        self.assertEqual("confirmed", row["tier"])
        self.assertEqual("123", row["advisor_crd"])

    def test_stated_crd_with_wrong_name_is_review_only(self):
        row = score_contacts(person("Bob Jones"), index()).iloc[0]
        self.assertEqual("none", row["tier"])
        self.assertEqual("123", row["proposed_crd"])

    def test_stated_crd_with_wrong_current_firm_is_review_only(self):
        row = score_contacts(person(firm="99"), index()).iloc[0]
        self.assertEqual("none", row["tier"])
        self.assertEqual("", row["advisor_crd"])
        self.assertEqual("stated_crd_current_firm_conflict",
                         row["match_reason"])

    def test_fuzzy_roster_candidate_is_hard_gated_to_firm_family(self):
        frame = person()
        frame["given_crd"] = ""
        frame["allowed_firm_crds"] = "99"
        row = score_contacts(frame, index()).iloc[0]
        self.assertEqual("none", row["tier"])
        self.assertEqual("", row["advisor_crd"])

    def test_multi_entity_family_accepts_either_current_sec_firm(self):
        frame = person()
        frame["allowed_firm_crds"] = "10|11"
        row = score_contacts(frame, index()).iloc[0]
        self.assertEqual("confirmed", row["tier"])


if __name__ == "__main__":
    unittest.main()
