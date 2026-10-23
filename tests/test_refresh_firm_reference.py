import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from refresh_firm_reference import reduce_firm_reference


class FirmReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "data/interim").mkdir(parents=True)
        pd.DataFrame({"crd": ["1", "2"]}).to_parquet(self.root / "data/interim/firms_scored.parquet", index=False)

    def cache(self, source, crd, record):
        path = self.root / "data" / source / (crd + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))
        return path

    def test_changed_alias_reduces_normalized_tables_without_network(self):
        source = self.cache("firm_api", "1", {"basicInformation": {"otherNames": ["Old Alias"]}})
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")):
            reduce_firm_reference(self.root)
            source.write_text(json.dumps({"basicInformation": {"otherNames": ["New Alias"]}}))
            result = reduce_firm_reference(self.root)
        for directory in ("data/interim", "data/output"):
            self.assertEqual(pd.read_parquet(self.root / directory / "firm_other_names.parquet").other_name.tolist(), ["New Alias"])
        self.assertFalse(result["retrieved"])

    def test_owners_and_missing_cache_reuse(self):
        self.cache("firm_owners", "1", {"crd": "1", "ok": True, "A": [{"owner_name": "Owner", "ownership_code": "E", "owner_crd": "123"}], "B": []})
        result = reduce_firm_reference(self.root)
        owners = pd.read_parquet(self.root / "data/output/firm_owners.parquet")
        self.assertEqual(owners.owner_name.tolist(), ["Owner"])
        self.assertEqual(owners.ownership_band.tolist(), ["75% or more"])
        (self.root / "data/firm_owners/1.json").unlink()
        result = reduce_firm_reference(self.root)
        self.assertEqual(result["sources"]["firm_owners"]["status"], "reused_derived")
        self.assertEqual(result["sources"]["firm_owners"]["reusedFirms"], 1)

    def test_bad_changed_cache_fails_without_replacing_outputs(self):
        path = self.cache("firm_api", "1", {"basicInformation": {"otherNames": ["Good"]}})
        reduce_firm_reference(self.root)
        before = (self.root / "data/output/firm_other_names.parquet").read_bytes()
        path.write_text('{"basicInformation":{"otherNames":"wrong type"}}')
        with self.assertRaises(ValueError):
            reduce_firm_reference(self.root)
        self.assertEqual((self.root / "data/output/firm_other_names.parquet").read_bytes(), before)

    def test_changed_failed_owner_cache_does_not_erase_prior(self):
        path = self.cache("firm_owners", "1", {"ok": True, "A": [{"owner_name": "Owner"}], "B": []})
        reduce_firm_reference(self.root)
        path.write_text('{"ok":false,"A":[],"B":[]}')
        with self.assertRaisesRegex(ValueError, "Changed ownership cache"):
            reduce_firm_reference(self.root)
        self.assertEqual(len(pd.read_parquet(self.root / "data/output/firm_owners.parquet")), 1)


if __name__ == "__main__":
    unittest.main()
