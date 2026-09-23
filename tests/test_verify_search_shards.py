"""Regression checks for stale desktop/field search location data."""
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from verify_search_shards import verify


class SearchShardIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.write("advisor_index.json", {
            "advisors": [["7320366", "Aaron Fayzulayev", 0, "NY", 0, "NY", ""]],
            "firms": ["Edward Jones"], "cities": ["Fresh Meadows"]})
        self.write("advisor_search.json", {
            "advisors": 1, "firms": ["Edward Jones"],
            "cities": ["Fresh Meadows"], "shards": ["fa"], "crdShards": ["732"]})
        self.desktop_row = ["7320366", "Aaron Fayzulayev", 0, "NY", 0, "NY", "", ""]
        self.write("search/fa.json", {"rows": [self.desktop_row]})
        self.write("search/crd/732.json", {"rows": [self.desktop_row]})
        self.write("pins_NY.json", {"pins": [[0, 0, 0, 0, 0, "", "7320366"]]})
        self.write("tile_index.json", {
            "columns": ["crd", "name", "city", "state"]})
        self.write("tiles/cell.json", {
            "cell": "cell", "rows": [["7320366", "Aaron Fayzulayev",
                                    "Fresh Meadows", "NY"]]})
        self.write("name_index.json", {"shards": ["fa"]})
        self.write("names/fa.json", {
            "rows": [["Aaron Fayzulayev", "7320366", "cell",
                      "Fresh Meadows", "NY", ""]]})

    def write(self, relative, payload):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_current_search_and_pins_pass(self):
        verify(self.root)

    def test_stale_desktop_search_state_fails(self):
        old = self.desktop_row.copy()
        old[3] = "MO"
        self.write("search/fa.json", {"rows": [old]})
        with self.assertRaisesRegex(RuntimeError, "stale search row"):
            verify(self.root)

    def test_stale_field_tile_state_fails(self):
        self.write("tiles/cell.json", {
            "cell": "cell", "rows": [["7320366", "Aaron Fayzulayev",
                                    "St. Louis", "MO"]]})
        with self.assertRaisesRegex(RuntimeError, "has no MO pin"):
            verify(self.root)


if __name__ == "__main__":
    unittest.main()
