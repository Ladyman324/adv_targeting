"""A held source record cannot transfer contact details to another CRD."""
import copy
import csv
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import build_contacts as contacts
from roster_identity_review import HOLD_FILE, HOLD_REASON, apply_roster_identity_holds, load_holds, write_review_report


class RosterIdentityReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "data/raw/firm_rosters/edward_jones_2026-10-26.csv"
        self.source.parent.mkdir(parents=True); self.source.write_text("Name,Email\nMichael Jones,michael.jones@example.com\n")
        self.hold = dict(source_slug="edward_jones", source_file=self.source.name,
                         source_sha256=hashlib.sha256(self.source.read_bytes()).hexdigest(),
                         name="Michael Jones", email="michael.jones@example.com", prior_crd="100")
        self.row = dict(source="Edward Jones", source_slug="edward_jones", source_file=self.source.name,
                        name="Michael Jones", email="michael.jones@example.com", advisor_crd="200", tier="high",
                        city="Atlanta", state="GA", firm_crd="250", allowed_firm_crds="250", authoritative_domain=True,
                        phone="4045550000", phone_kind="direct", profile_url="https://example.com/michael-jones",
                        identity_status="approved", can_email=True, can_act_write=False)

    def save(self, hold=None):
        path = self.root / HOLD_FILE; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schemaVersion":1,"holds":[hold or self.hold]}))

    def test_no_review_file_leaves_all_fields_unchanged(self):
        original = pd.DataFrame([self.row])
        pd.testing.assert_frame_equal(original, apply_roster_identity_holds(original, self.root))

    def test_changed_crd_is_held_without_rewriting_identity_or_permissions(self):
        self.save(); original = pd.DataFrame([self.row]); before = original.copy(deep=True)
        result = apply_roster_identity_holds(original, self.root)
        self.assertEqual("", result.iloc[0].advisor_crd)
        self.assertEqual("200", result.iloc[0].proposed_crd)
        self.assertEqual("review", result.iloc[0].tier)
        self.assertEqual(HOLD_REASON, result.iloc[0].match_reason)
        for field in ["identity_status","can_email","can_act_write","name","email","phone"]:
            self.assertEqual(original.iloc[0][field], result.iloc[0][field])
        self.assertEqual(1,len(result.attrs["roster_identity_review"]))
        pd.testing.assert_frame_equal(original,before)

    def test_previous_crd_and_other_records_keep_their_existing_authority(self):
        self.save()
        rows=[dict(self.row,advisor_crd="100"), dict(self.row,name="Anne Jones",email="anne.jones@example.com"),
              dict(self.row,email="other@example.com"), dict(self.row,source="CRM"), dict(self.row,source="EIC")]
        before=pd.DataFrame(rows);after=apply_roster_identity_holds(before,self.root)
        pd.testing.assert_frame_equal(before,after)
        self.assertEqual([],after.attrs["roster_identity_review"])

    def test_a_third_crd_cannot_bypass_an_unresolved_hold(self):
        self.save();after=apply_roster_identity_holds(pd.DataFrame([dict(self.row,advisor_crd="300")]),self.root)
        self.assertEqual("",after.iloc[0].advisor_crd);self.assertEqual("300",after.iloc[0].proposed_crd)

    def test_duplicate_roster_witness_cannot_bypass_disputed_email_hold(self):
        self.save()
        row=dict(self.row,source_file="edward_jones_other_snapshot.csv",name="M. Jones")
        after=apply_roster_identity_holds(pd.DataFrame([row]),self.root)
        self.assertEqual("",after.iloc[0].advisor_crd)
        self.assertEqual("200",after.iloc[0].proposed_crd)

    def test_email_hold_never_takes_authority_from_the_prior_owner(self):
        self.save()
        rows=[dict(self.row,source_file="edward_jones_other_snapshot.csv",advisor_crd="100"),
              dict(self.row,source_slug="",source="Other source")]
        before=pd.DataFrame(rows);after=apply_roster_identity_holds(before,self.root)
        pd.testing.assert_frame_equal(before,after)

    def test_source_bytes_must_still_match_the_review(self):
        self.save();self.source.write_text("changed source")
        with self.assertRaisesRegex(ValueError,"source bytes changed"):load_holds(self.root)

    def test_invalid_or_traversing_review_cannot_supply_a_hold(self):
        for change in [{"source_file":"../other.csv"},{"prior_crd":"0"},{"prior_crd":"not-a-crd"},
                       {"source_sha256":"bad"},{"source_slug":"CRM"},{"name":""}]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.save(dict(self.hold,**change));load_holds(self.root)

    def test_actual_scorer_holds_before_anchor_resolution(self):
        self.save(); index={"jones":[("200",{"michael"},{"firms":{"250"},"cities":{"atlanta"},"states":{"GA"},"suffix":""})]}
        with mock.patch.object(contacts,"ROOT",self.root):
            scored=contacts.score_contacts(pd.DataFrame([self.row]),index)
            after=contacts.apply_anchor_residual_pass(scored,index)
        self.assertEqual("",after.iloc[0].advisor_crd)
        self.assertEqual(HOLD_REASON,after.iloc[0].match_reason)
        self.assertIsNone(contacts._strict_residual_context(after.iloc[0],index))
        self.assertEqual(1,len(after.attrs["roster_identity_review"]))

    def test_report_is_reviewable_and_spreadsheet_cells_are_escaped(self):
        self.save();after=apply_roster_identity_holds(pd.DataFrame([self.row]),self.root)
        rows=copy.deepcopy(after.attrs["roster_identity_review"]);rows[0]["name"]="=FORMULA"
        path=self.root/"review.csv";write_review_report(path,rows)
        with path.open(encoding="utf-8-sig",newline="") as stream:report=list(csv.DictReader(stream))
        self.assertEqual("'=FORMULA",report[0]["name"])
        self.assertEqual("100",report[0]["prior_crds"]);self.assertEqual("200",report[0]["proposed_crd"])


if __name__ == "__main__": unittest.main()
