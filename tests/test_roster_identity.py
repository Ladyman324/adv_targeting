from __future__ import annotations

import pathlib
import sys
import unittest

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from build_contacts import (ANCHOR_RESIDUAL_REASON,
                            apply_anchor_residual_pass, score_contacts)
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


def shaw_row(name, email, profile, phone, city="Memphis", state="TN"):
    return {
        "source": "Raymond James", "source_slug": "rj_branches",
        "given_crd": "", "name": name, "email": email,
        "firm_crd": "705", "allowed_firm_crds": "705",
        "city": city, "state": state, "authoritative_domain": True,
        "profile_url": profile, "phone": phone, "phone_kind": "direct",
    }


def shaw_index(extra=None):
    location = {"firms": {"705"}, "cities": {"memphis"},
                "states": {"TN"}}
    rows = [
        ("856092", {"lynn", "trusty", "t"}, {**location, "suffix": ""}),
        ("5281870", {"lynn", "trusty", "t"}, {**location, "suffix": "ii"}),
    ]
    if extra:
        rows.extend(extra)
    return {"shaw": rows}


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


class AnchorResidualTests(unittest.TestCase):
    def shaw_people(self):
        # Two source witnesses per person. They are one roster identity apiece
        # because each pair shares its authoritative personal email.
        father = shaw_row(
            "Lynn T. Shaw", "lynn.shaw@raymondjames.com",
            "https://raymondjames.com/profile?aid=728705", "9017667738")
        son = shaw_row(
            "Lynn T. Shaw II", "lynn.shawii@raymondjames.com",
            "https://raymondjames.com/profile?aid=728668", "9018187683")
        return pd.DataFrame([father, dict(father), son, dict(son)])

    def test_lynn_shaw_resolves_from_one_immutable_suffix_anchor(self):
        first = score_contacts(self.shaw_people(), shaw_index())
        father = first[first["email"].str.startswith("lynn.shaw@")]
        son = first[first["email"].str.startswith("lynn.shawii@")]
        self.assertEqual({"review"}, set(father["tier"]))
        self.assertEqual({0.0}, set(father["match_gap"]))
        self.assertEqual({"high"}, set(son["tier"]))
        self.assertEqual({"5281870"}, set(son["advisor_crd"]))
        self.assertEqual({0.18}, set(son["match_gap"]))

        resolved = apply_anchor_residual_pass(first, shaw_index())
        father = resolved[resolved["email"].str.startswith("lynn.shaw@")]
        self.assertEqual({"high"}, set(father["tier"]))
        self.assertEqual({"856092"}, set(father["advisor_crd"]))
        self.assertEqual({ANCHOR_RESIDUAL_REASON}, set(father["match_reason"]))
        self.assertEqual({"5281870"}, set(father["match_anchors"]))

    def test_leading_initial_uses_same_first_noninitial_block(self):
        people = self.shaw_people()
        people["name"] = people["name"].str.replace(
            "Lynn T.", "W. Lynn", regex=False)
        first = score_contacts(people, shaw_index())
        resolved = apply_anchor_residual_pass(first, shaw_index())
        father = resolved[resolved["email"].str.startswith("lynn.shaw@")]
        self.assertEqual({"856092"}, set(father["advisor_crd"]))
        self.assertEqual({ANCHOR_RESIDUAL_REASON}, set(father["match_reason"]))

    def test_requires_authoritative_domain_and_person_specific_signal(self):
        for change in (
                {"authoritative_domain": False},
                {"profile_url": "", "phone_kind": "shared"}):
            with self.subTest(change=change):
                people = self.shaw_people()
                mask = people["email"].str.startswith("lynn.shaw@")
                for key, value in change.items():
                    people.loc[mask, key] = value
                first = score_contacts(people, shaw_index())
                resolved = apply_anchor_residual_pass(first, shaw_index())
                father = resolved[
                    resolved["email"].str.startswith("lynn.shaw@")]
                self.assertEqual({"review"}, set(father["tier"]))

    def test_requires_exact_city_and_state(self):
        people = self.shaw_people()
        mask = people["email"].str.startswith("lynn.shaw@")
        people.loc[mask, "city"] = "Nashville"
        first = score_contacts(people, shaw_index())
        resolved = apply_anchor_residual_pass(first, shaw_index())
        self.assertEqual({"review"}, set(resolved.loc[mask, "tier"]))

    def test_unbalanced_one_person_two_crds_is_not_forced(self):
        people = self.shaw_people().iloc[:2].copy()
        first = score_contacts(people, shaw_index())
        resolved = apply_anchor_residual_pass(first, shaw_index())
        self.assertEqual({"review"}, set(resolved["tier"]))

    def test_release_one_refuses_larger_components_and_cannot_cascade(self):
        people = self.shaw_people()
        third = shaw_row(
            "Lynn Trusty Shaw", "lynn.shaw3@raymondjames.com",
            "https://raymondjames.com/profile?aid=third", "9015550103")
        people = pd.concat([people, pd.DataFrame([third])], ignore_index=True)
        extra = [("9999999", {"lynn", "trusty", "t"}, {
            "firms": {"705"}, "cities": {"memphis"}, "states": {"TN"},
            "suffix": ""})]
        idx = shaw_index(extra)
        first = score_contacts(people, idx)
        resolved = apply_anchor_residual_pass(first, idx)
        self.assertNotIn(ANCHOR_RESIDUAL_REASON,
                         set(resolved["match_reason"]))

    def test_email_local_part_only_high_match_cannot_be_an_anchor(self):
        people = self.shaw_people()
        son = people["email"].str.startswith("lynn.shawii@")
        people.loc[son, "name"] = "C. Shaw II"
        people.loc[son, "email"] = "lynn2.shaw@raymondjames.com"
        first = score_contacts(people, shaw_index())
        resolved = apply_anchor_residual_pass(first, shaw_index())
        self.assertNotIn(ANCHOR_RESIDUAL_REASON,
                         set(resolved["match_reason"]))


if __name__ == "__main__":
    unittest.main()
