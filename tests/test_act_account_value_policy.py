import contextlib, copy, csv, io, json, pathlib, sys, tempfile, unittest
from unittest import mock
import pandas as pd
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from act_book import ACCOUNT_VALUE_POLICY, account_value_conflicts, build
import build_act_assets as assets
from build_act_economic_links import write_account_value_review, validate_act_ids
from refresh_reports import _assets

def contact(aid, value, allcap=None, large=0, edited="2026-10-30T13:08:57Z"):
    return {"id": aid, "fullName": aid, "edited": edited, "customFields": {
        "bcode1": "ML9668", "bvalue_1": value, "ballcap_1": value if allcap is None else allcap,
        "blarge_1": large, "bupdate": "2026-09-30"}}

class AccountValuePolicyTests(unittest.TestCase):
    def test_ml9668_summary_deduplication_and_source_immutability(self):
        rows = [contact("David",3573346.02),contact("Bill",3324114.13),
                contact("Wade",3324114.13,edited="2026-10-30T13:08:51Z")]
        original = copy.deepcopy(rows)
        accounts, conflicts, ties = build(rows)
        self.assertEqual(1,len(accounts))
        self.assertEqual(3573346.02,accounts["ML9668"]["value"])
        self.assertEqual(3,accounts["ML9668"]["holders"])
        self.assertEqual(["ML9668"],ties)
        self.assertEqual(1,len(conflicts))
        issue = account_value_conflicts(rows)[0]
        self.assertEqual("warning",issue["severity"])
        self.assertFalse(issue["publicationBlocking"])
        self.assertEqual(3,len(issue["copies"]))
        self.assertEqual(original,rows)

    def test_higher_value_wins_even_when_lower_copy_is_newer(self):
        rows=[contact("high",100,edited="2026-10-01"),contact("low",80,edited="2026-11-01")]
        self.assertEqual(100,build(rows)[0]["ML9668"]["value"])
        self.assertEqual(80,build([rows[1]])[0]["ML9668"]["value"])

    def test_selects_whole_allocation_without_combining_product_maxima(self):
        rows=[contact("acv",100),contact("lcv",90,allcap=0,large=90)]
        self.assertEqual([100,0,0,0,100],assets.account_vector(build(rows)[0]["ML9668"]))

    def test_equal_total_allocation_conflict_is_flagged_and_order_independent(self):
        rows=[contact("a",100),contact("b",100,allcap=0,large=100)]
        self.assertEqual(build(rows),build(list(reversed(rows))))
        self.assertEqual(1,len(account_value_conflicts(rows)))

    def test_private_report_keeps_copies_and_escapes_spreadsheet_cells(self):
        rows=[contact("=unsafe",100),contact("low",80)]
        with tempfile.TemporaryDirectory() as td:
            path=pathlib.Path(td)/"review.csv"
            with mock.patch("build_act_economic_links.ACCOUNT_VALUE_REVIEW_CSV",path), contextlib.redirect_stdout(io.StringIO()):
                issues=write_account_value_review(rows)
            with path.open(encoding="utf-8-sig",newline="") as stream:
                report=list(csv.DictReader(stream))
            self.assertEqual(2,len(report))
            self.assertEqual({"100.0"},{r["selected_value"] for r in report})
            self.assertTrue(any(r["contact_name"]=="'=unsafe" for r in report))
            self.assertEqual(1,len(issues))

    def payload(self,deployed=("100","200","300"),values=(100,80,80)):
        rows=[contact(aid,value) for aid,value in zip(("a","b","c"),values)]
        links=pd.DataFrame([dict(act_id=aid,advisor_crd=crd,economic_status="approved",link_type="identity_approved")
                            for aid,crd in zip(("a","b","c"),("100","200","300"))])
        original=copy.deepcopy(rows)
        with tempfile.TemporaryDirectory() as td:
            path=pathlib.Path(td)/"act_assets.json"
            with mock.patch.object(assets,"OUT",path), mock.patch.object(assets,"load_economic_links",
                    return_value=(pathlib.Path("act.json"),rows,links,{"contentHash":"economic","reconciliation":{}})), mock.patch.object(
                    assets,"load_deployed_advisor_crds",return_value=(set(deployed),{})), contextlib.redirect_stdout(io.StringIO()):
                assets.main()
            data=json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(original,rows)
        return data

    def test_publication_preserves_advisor_amounts_and_one_maximum_summary(self):
        data=self.payload()
        self.assertEqual(100,data["advisors"]["100"]["acv"])
        self.assertEqual(80,data["advisors"]["200"]["acv"])
        self.assertEqual(80,data["advisors"]["300"]["av"]["0"][0])
        self.assertEqual(1,data["totals"]["accounts"])
        self.assertEqual(100,data["totals"]["acv"])
        self.assertEqual(ACCOUNT_VALUE_POLICY,data["account_value_policy"])
        self.assertEqual([],_assets(data)["issues"])

    def test_off_map_higher_copy_does_not_inflate_mapped_summary(self):
        data=self.payload(deployed=("100","200"),values=(100,80,200))
        self.assertEqual(100,data["totals"]["acv"])
        self.assertEqual(200,data["source_totals"]["acv"])
        self.assertEqual(100,data["deployment_reconciliation"]["source_holder_value_difference"]["acv"])
        self.assertEqual([],_assets(data)["issues"])

    def test_field_tiles_preserve_each_advisors_recorded_value(self):
        from build_field_tiles import canonical_asset_amounts
        data=self.payload()
        self.assertEqual({"100":100,"200":80,"300":80},canonical_asset_amounts(data))

    def test_field_tiles_use_source_total_once_and_accept_legacy_vectors(self):
        from build_field_tiles import canonical_asset_amounts
        data={"columns":["acv","lcv","mf","midcap"],
              "accounts":[[100,20,0,0,150],[5,0,0,0]],
              "advisors":{"1":{"t":"high","ix":[0,0,1]},
                          "2":{"t":"confirmed","ix":[0],"av":{"0":[30,20,0,0,60]}}}}
        self.assertEqual({"1":155,"2":60},canonical_asset_amounts(data))

    def test_field_tiles_reject_invalid_advisor_overrides(self):
        from build_field_tiles import canonical_asset_amounts
        data={"columns":["acv","lcv","mf","midcap"],"accounts":[[100,0,0,0,100]],
              "advisors":{"1":{"t":"high","ix":[0]}}}
        for override in [None,[],{"1":[80,0,0,0,80]},{"00":[80,0,0,0,80]},
                         {"0":[80,0,0,0,True]},{"0":[80,0,0,0,-1]},
                         {"0":[80,0,0,0,float("nan")]},{"0":[80,0,0,0,float("inf")]},
                         {"0":[80,0,0]},{"0":"80"}]:
            with self.subTest(override=override),self.assertRaises(ValueError):
                current=copy.deepcopy(data);current["advisors"]["1"]["av"]=override
                canonical_asset_amounts(current)

    def test_duplicate_act_identifiers_remain_a_hard_failure(self):
        with self.assertRaises(SystemExit):
            validate_act_ids([contact("same",100),contact("same",80)])

if __name__=="__main__":unittest.main()
