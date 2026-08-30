from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
import zipfile

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from forbes_match import build_index
from parse_advisors import parse
from sec_names import (aliases_by_crd, build_nickname_evidence,
                       load_advisor_aliases, surname_given_groups)


class SecAliasTests(unittest.TestCase):
    def test_parser_preserves_every_other_name_without_changing_legacy_return(self):
        xml = b'''<IAPDIndividualReport GenDt="2026-07-20">
          <Indvls><Indvl><Info indvlPK="4784023" firstNm="NICOLE"
            lastNm="FLORES" actvAGReg="Y"/>
            <OthrNms>
              <OthrNm firstNm="NICOLE" lastNm="FLORES"/>
              <OthrNm firstNm="NICOLE" midNm="TERESSA" lastNm="FLORES"/>
              <OthrNm firstNm="NICOLE" lastNm="TESORIERO"/>
            </OthrNms></Indvl></Indvls></IAPDIndividualReport>'''
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "feed.zip"
            with zipfile.ZipFile(path, "w") as zf:
                zf.writestr("sample.xml", xml)
            self.assertEqual(5, len(parse(path)))
            frames = parse(path, include_other_names=True)
        advisors, aliases = frames[0], frames[5]
        self.assertEqual(3, len(aliases))
        self.assertEqual([1, 2, 3], aliases["alias_ordinal"].tolist())
        self.assertEqual("TESORIERO", aliases.iloc[2]["last_name"])
        # A surname change is an identity alias, not a new preferred first name.
        self.assertEqual("", advisors.iloc[0]["used_first_name"])

    def test_surname_groups_do_not_flatten_given_names_across_alias_rows(self):
        row = type("Advisor", (), {
            "advisor_crd": "4784023", "first_name": "NICOLE",
            "middle_name": "TERESSA", "used_first_name": "",
            "last_name": "FLORES", "last_key": "flores",
        })()
        aliases = [{"first_name": "NICOLE", "middle_name": "",
                    "last_name": "TESORIERO", "suffix": ""}]
        groups = surname_given_groups(row, aliases)
        self.assertEqual({"nicole", "teressa"}, groups["flores"])
        self.assertEqual({"nicole"}, groups["tesoriero"])

    def test_build_index_reaches_crd_by_legal_and_alternate_surname(self):
        advisors = pd.DataFrame([{
            "advisor_crd": "4784023", "first_name": "NICOLE",
            "middle_name": "TERESSA", "last_name": "FLORES", "suffix": "",
            "used_first_name": "", "last_key": "flores",
        }])
        branches = pd.DataFrame([{
            "advisor_crd": "4784023", "firm_crd": "8174",
            "branch_city": "NEW YORK", "branch_state": "NY",
        }])
        employment = pd.DataFrame(columns=[
            "advisor_crd", "firm_name_on_record", "city", "state"])
        aliases = aliases_by_crd(pd.DataFrame([{
            "advisor_crd": "4784023", "alias_ordinal": 1,
            "first_name": "NICOLE", "middle_name": "",
            "last_name": "TESORIERO", "suffix": "",
        }]))
        index = build_index(advisors, branches, employment, aliases)
        self.assertEqual("4784023", index["tesoriero"][0][0])
        self.assertIn("nicole", index["tesoriero"][0][1])
        self.assertNotIn("teressa", index["tesoriero"][0][1])
        self.assertEqual("4784023", index["flores"][0][0])

    def test_nickname_evidence_is_same_surname_distinct_person_support_only(self):
        advisors = pd.DataFrame([
            {"advisor_crd": "1", "first_name": "WILLIAM", "last_name": "SMITH"},
            {"advisor_crd": "2", "first_name": "NICOLE", "last_name": "FLORES"},
        ])
        aliases = pd.DataFrame([
            {"advisor_crd": "1", "first_name": "BILL", "middle_name": "",
             "last_name": "SMITH", "suffix": ""},
            {"advisor_crd": "1", "first_name": "BILL", "middle_name": "",
             "last_name": "SMITH", "suffix": ""},
            {"advisor_crd": "1", "first_name": "B", "middle_name": "",
             "last_name": "SMITH", "suffix": ""},
            {"advisor_crd": "2", "first_name": "NICOLE", "middle_name": "",
             "last_name": "TESORIERO", "suffix": ""},
        ])
        evidence = build_nickname_evidence(advisors, aliases)
        self.assertEqual(1, len(evidence))
        self.assertEqual("william", evidence.iloc[0]["legal_first_name"])
        self.assertEqual("bill", evidence.iloc[0]["alternate_first_name"])
        self.assertEqual(1, evidence.iloc[0]["distinct_crd_support"])
        self.assertFalse(evidence.iloc[0]["automatic_match"])

    def test_missing_alias_artifact_is_backward_compatible(self):
        self.assertEqual({}, load_advisor_aliases(
            pathlib.Path("this-file-does-not-exist.parquet")))


if __name__ == "__main__":
    unittest.main()
