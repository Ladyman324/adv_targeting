from pathlib import Path
import gzip
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from refresh_geography import building_keys, unambiguous_points, COORDINATES
from firm_universe import supplement_registered_firms
from identity_audit import corroborated_name_pairs
from identity_schema import EVIDENCE_FILENAME, LINKS_FILENAME, MANIFEST_FILENAME, content_hash
from contact_provenance import sha256_file
import test_refresh_improvements as name_fixtures
from identity_resolver import evaluate_assertion
import refresh_core


class CachedBuildingTests(unittest.TestCase):
    def row(self, **changes):
        return {"branch_street1":"100 MAIN ST", "branch_street2":"SUITE 1",
                "branch_city":"CITY", "branch_state":"GA", "branch_postal":"12345-6789", **changes}

    def test_only_suite_and_postal_extension_are_relaxed(self):
        baseline = building_keys(pd.DataFrame([self.row()])).iloc[0]
        same = self.row(branch_street2="SUITE 999", branch_postal="12345")
        self.assertEqual(baseline, building_keys(pd.DataFrame([same])).iloc[0])
        formatted = self.row(branch_street1="100 Main Street, Suite 4",branch_postal="12345")
        self.assertEqual(baseline,building_keys(pd.DataFrame([formatted])).iloc[0])
        for change in ({"branch_street1":"101 MAIN ST"}, {"branch_city":"OTHER"},
                       {"branch_state":"FL"}, {"branch_postal":"54321"}):
            self.assertNotEqual(baseline, building_keys(pd.DataFrame([self.row(**change)])).iloc[0])
        north = building_keys(pd.DataFrame([self.row(branch_street1="100 MAIN ST NORTH")])).iloc[0]
        self.assertNotEqual(baseline,north)
        self.assertNotEqual(baseline,building_keys(pd.DataFrame([self.row(branch_street1="100 MAIN ST BLDG 2")])).iloc[0])
        for change in ({"branch_street1":"PO BOX 100"}, {"branch_postal":""},
                       {"branch_city":""}, {"branch_postal":"12345-BAD"}):
            self.assertTrue(pd.isna(building_keys(pd.DataFrame([self.row(**change)])).iloc[0]))

    def test_conflicting_coordinates_are_not_selected_by_file_order(self):
        rows = pd.DataFrame([{"key":"same", "lat":33., "lon":-84.},
                             {"key":"same", "lat":34., "lon":-84.},
                             {"key":"safe", "lat":33., "lon":-84.}]).reindex(columns=["key"]+COORDINATES)
        self.assertEqual(["safe"], unambiguous_points(rows, "key").key.tolist())

    def test_atomic_json_retries_windows_sharing_but_not_other_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'run.json'
            target.write_text('old')
            error = PermissionError('sharing'); error.winerror = 32
            original = refresh_core.os.replace
            with patch.object(refresh_core.os,'replace',side_effect=[error,None]) as replace, \
                    patch.object(refresh_core.time,'sleep'):
                refresh_core.atomic_json(target,{'new':True})
                self.assertEqual(2,replace.call_count)
                self.assertEqual('old',target.read_text())
            with patch.object(refresh_core.os,'replace',side_effect=PermissionError('denied')) as replace:
                with self.assertRaises(PermissionError):refresh_core.atomic_json(target,{'new':True})
                self.assertEqual(1,replace.call_count)


class FirmUniverseTests(unittest.TestCase):
    def test_only_current_registered_employers_supplement_missing_adv(self):
        with tempfile.TemporaryDirectory() as directory:
            feed = Path(directory)/"firms.gz"
            records = []
            for crd, kind, status in [("1","Registered","APPROVED"), ("2","Registered","APPROVED"),
                                      ("3","Exempt Reporting Adviser","APPROVED"),
                                      ("4","Registered","TERMINATED"), ("5","Registered","APPROVED")]:
                records.append(f'<Firm><Info FirmCrdNb="{crd}" BusNm="Firm {crd}"/><Rgstn FirmType="{kind}" St="{status}"/><MainAddr State="GA"/></Firm>')
            with gzip.open(feed,"wt") as handle: handle.write('<Firms>'+''.join(records)+'</Firms>')
            scored = pd.DataFrame([{"crd":"1","name":"Retained", "state":"GA", "firm_display":"Retained",
                "raum_total":100., "opportunity_score":50., "is_target":True, "motion":"sma_led"}])
            jobs = pd.DataFrame([{"advisor_crd":str(i), "firm_crd":str(i)} for i in range(1,5)])
            result = supplement_registered_firms(scored, feed, jobs).set_index("crd")
            self.assertEqual({"1","2"},set(result.index))
            self.assertEqual(100., result.loc["1","raum_total"])
            self.assertTrue(pd.isna(result.loc["2","raum_total"]))
            self.assertFalse(result.loc["2","adv_financials_available"])
            self.assertEqual("insufficient_data",result.loc["2","firm_type"])
            self.assertEqual(1,result.loc["2","n_advisors"])


class BoundNameAuditTests(unittest.TestCase):
    def test_bridge_is_bound_to_pair_and_artifacts(self):
        record, sec, context = name_fixtures.CorroboratedNames().case()
        evidence, link = evaluate_assertion(record, sec, context)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); sec_path = root/'advisors.parquet'
            pd.DataFrame([sec]).to_parquet(sec_path,index=False)
            def write(ev=evidence, decision=link):
                pd.DataFrame([ev]).to_parquet(root/EVIDENCE_FILENAME,index=False)
                pd.DataFrame([decision]).to_parquet(root/LINKS_FILENAME,index=False)
                manifest = {"outputs":{name:{"sha256":sha256_file(root/name)} for name in (EVIDENCE_FILENAME,LINKS_FILENAME)},
                            "secSources":{sec_path.name:sha256_file(sec_path)}}
                (root/MANIFEST_FILENAME).write_text(json.dumps({**manifest,"contentHash":content_hash(manifest)}))
            write()
            self.assertEqual({("123","fixture")},corroborated_name_pairs(root,sec_path))
            for change in ({"advisor_crd":"456"},{"assertion_evidence_hash":"wrong"},{"identity_status":"review"}):
                write(decision={**link,**change})
                self.assertEqual(set(),corroborated_name_pairs(root,sec_path))
            write(ev={**evidence,"firm_current_agrees":False})
            self.assertEqual(set(),corroborated_name_pairs(root,sec_path))
            write()
            pd.DataFrame([{**evidence,"given_corroborated":False}]).to_parquet(root/EVIDENCE_FILENAME,index=False)
            with self.assertRaisesRegex(ValueError,'manifest-bound'):corroborated_name_pairs(root,sec_path)
            write()
            pd.DataFrame([{**sec,"first_name":"Different"}]).to_parquet(sec_path,index=False)
            with self.assertRaisesRegex(ValueError,'different SEC'):corroborated_name_pairs(root,sec_path)


if __name__ == '__main__': unittest.main()
