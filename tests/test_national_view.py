from __future__ import annotations

import copy
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import enrich_national_opportunity as enrich  # noqa: E402
import validate_webapp_data as validate  # noqa: E402


def detail_fixture() -> dict:
    return {
        "states": ["CA", "GA", "AK"],
        "firms": [
            ["Selecting Firm", "101", 1, 10, 1, 2, 3, 1],
            ["Other Firm", "202", 1, 10, 0, 2, 3, 1],
        ],
        "offices": [
            [-120.01, 35.01, 3, 0, 0, 2, 0, 10],
            [-119.99, 34.99, 4, 1, 0, 2, 0, 10],
            [-84.25, 33.75, 2, 1, 0, 1, 1, 11],
            [-120.01, 35.01, 5, 0, 0, 1, 2, 12],
        ],
    }


class NationalViewGeneratorTests(unittest.TestCase):
    def test_compact_schema_preserves_all_and_default_selecting_totals(self):
        view = enrich.build_national_view(detail_fixture())

        self.assertEqual({"states", "grid"}, set(view))
        self.assertNotIn("firms", view)
        self.assertEqual(14, sum(cell[2] for cell in view["grid"]))
        self.assertEqual(8, sum(cell[3] for cell in view["grid"]))
        self.assertEqual([
            [35.0, -120.0, 7, 3, 0],
            [33.75, -84.25, 2, 0, 1],
            [35.0, -120.0, 5, 5, 2],
        ], view["grid"])

    def test_same_coordinate_in_different_states_stays_separate(self):
        view = enrich.build_national_view(detail_fixture())
        same_place = [cell for cell in view["grid"]
                      if cell[0] == 35.0 and cell[1] == -120.0]
        self.assertEqual([0, 2], [cell[4] for cell in same_place])
        self.assertEqual(["CA", "AK"],
                         [view["states"][cell[4]] for cell in same_place])

    def test_writer_emits_the_compact_payload(self):
        national = detail_fixture()
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(enrich, "WEB", pathlib.Path(td)):
            enrich.write_national_view(national)
            written = json.loads(
                (pathlib.Path(td) / "national_view.json").read_text(
                    encoding="utf-8"))
        self.assertEqual(enrich.build_national_view(national), written)


class NationalViewValidatorTests(unittest.TestCase):
    def test_accepts_exact_detail_equivalent_view(self):
        national = detail_fixture()
        validate.validate_national_view(
            enrich.build_national_view(national), national)

    def test_rejects_legacy_firm_dictionary(self):
        national = detail_fixture()
        view = enrich.build_national_view(national)
        view["firms"] = national["firms"]
        with self.assertRaisesRegex(SystemExit, "only states and grid"):
            validate.validate_national_view(view, national)

    def test_rejects_state_order_or_index_drift(self):
        national = detail_fixture()
        wrong_order = enrich.build_national_view(national)
        wrong_order["states"] = list(reversed(wrong_order["states"]))
        with self.assertRaisesRegex(SystemExit, "ordered states"):
            validate.validate_national_view(wrong_order, national)

        bad_index = enrich.build_national_view(national)
        bad_index["grid"][0][4] = len(bad_index["states"])
        with self.assertRaisesRegex(SystemExit, "invalid state index"):
            validate.validate_national_view(bad_index, national)

    def test_rejects_default_selecting_total_drift(self):
        national = detail_fixture()
        view = enrich.build_national_view(national)
        view["grid"][0][3] += 1
        with self.assertRaisesRegex(SystemExit, "selecting-count totals"):
            validate.validate_national_view(view, national)

    def test_rejects_detail_drift_even_when_national_totals_match(self):
        national = detail_fixture()
        view = copy.deepcopy(enrich.build_national_view(national))
        view["grid"][0][2] -= 1
        view["grid"][1][2] += 1
        with self.assertRaisesRegex(SystemExit, "per-state aggregate"):
            validate.validate_national_view(view, national)


if __name__ == "__main__":
    unittest.main()
