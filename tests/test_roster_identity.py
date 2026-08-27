from __future__ import annotations

import pathlib
import sys
import unittest

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from build_contacts import score_contacts


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
    def test_stated_crd_requires_matching_sec_name_and_firm(self):
        row = score_contacts(person(), index()).iloc[0]
        self.assertEqual("confirmed", row["tier"])
        self.assertEqual("123", row["advisor_crd"])

    def test_stated_crd_with_wrong_name_is_review_only(self):
        row = score_contacts(person("Bob Jones"), index()).iloc[0]
        self.assertEqual("review", row["tier"])

    def test_stated_crd_with_wrong_current_firm_is_review_only(self):
        row = score_contacts(person(firm="99"), index()).iloc[0]
        self.assertEqual("review", row["tier"])


if __name__ == "__main__":
    unittest.main()
