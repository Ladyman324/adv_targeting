from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock

import pandas as pd


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import apply_overrides  # noqa: E402
import branch_geography  # noqa: E402
import geocode  # noqa: E402
from geography_integrity import (  # noqa: E402
    returned_state,
    state_agrees,
    validate_geocoded_frame,
)


class GeocoderStateTests(unittest.TestCase):
    def test_parses_census_and_google_state_formats(self):
        self.assertEqual("TX", returned_state(
            "100 W PARK AVE, ORANGE, TX, 77630"))
        self.assertEqual("OH", returned_state(
            "100 Park Ave, Beachwood, OH 44122, USA"))
        self.assertEqual("OH", returned_state("Orange Village, Ohio, USA"))
        self.assertEqual("AZ", returned_state(
            "Scottsdale, Maricopa County, Arizona, 85254, United States"))
        self.assertEqual("DC", returned_state(
            "Washington, District of Columbia, 20005, United States"))
        self.assertTrue(state_agrees(
            "4401 KALANI DR, DIAMONDHEAD, MS, 39525", "ms"))

    def test_tier1_rejects_cross_state_and_unparseable_matches(self):
        unique = pd.DataFrame([
            {"id": "0", "addr_key": "good", "branch_street1": "100 PARK AVE",
             "branch_street2": "", "branch_city": "BEACHWOOD",
             "branch_postal": "44122", "geo_street": "100 PARK AVE"},
            {"id": "1", "addr_key": "wrong", "branch_street1": "100 PARK AVE",
             "branch_street2": "", "branch_city": "ORANGE VILLAGE",
             "branch_postal": "44122", "geo_street": "100 PARK AVE"},
            {"id": "2", "addr_key": "unknown", "branch_street1": "1 MAIN ST",
             "branch_street2": "", "branch_city": "COLUMBUS",
             "branch_postal": "43004", "geo_street": "1 MAIN ST"},
        ])
        response = pd.DataFrame([
            ["0", "", "Match", "Exact", "100 PARK AVE, BEACHWOOD, OH, 44122",
             "-81.49,41.46", "", ""],
            ["1", "", "Match", "Non_Exact", "100 W PARK AVE, ORANGE, TX, 77630",
             "-93.73,30.10", "", ""],
            ["2", "", "Match", "Exact", "UNPARSEABLE RESULT",
             "-82.90,40.10", "", ""],
        ], columns=geocode.COLS)
        with mock.patch.object(geocode, "_post", return_value=response):
            result = geocode.tier1(unique, "OH")
        self.assertEqual("rooftop", result.loc[result.id.eq("0"),
                                                "geocode_precision"].iloc[0])
        self.assertTrue(result.loc[result.id.isin(["1", "2"]),
                                   "geocode_precision"].isna().all())

    def test_tier2_requires_returned_state_as_well_as_street_and_zip(self):
        unique = pd.DataFrame([
            {"id": "0", "addr_key": "a", "geo_street": "4401 KALANI DR",
             "branch_city": "DIAMONDHEAD", "branch_postal": "39525",
             "zip5": "39525"},
        ])
        wrong = pd.DataFrame([
            ["0#2", "", "Match", "Exact",
             "4403 KALANI DR, DIAMONDHEAD, IL, 39525", "-89.37,30.37", "", ""],
        ], columns=geocode.COLS)
        with mock.patch.object(geocode, "_post", return_value=wrong):
            self.assertTrue(geocode.tier2(unique, {"a"}, "MS").empty)

        right = wrong.copy()
        right.loc[0, "matched"] = "4403 KALANI DR, DIAMONDHEAD, MS, 39525"
        with mock.patch.object(geocode, "_post", return_value=right):
            self.assertEqual(1, len(geocode.tier2(unique, {"a"}, "MS")))

    def test_export_gate_rejects_shard_and_explicit_result_conflicts(self):
        frame = pd.DataFrame([{
            "advisor_crd": "1", "branch_street1": "100 PARK AVE",
            "branch_city": "ORANGE VILLAGE", "branch_state": "OH",
            "lat": 30.1, "lon": -93.7,
            "matched": "100 W PARK AVE, ORANGE, TX, 77630",
        }])
        with self.assertRaisesRegex(ValueError, "conflict with state shard OH"):
            validate_geocoded_frame(frame, "OH")
        frame.loc[0, "matched"] = "100 PARK AVE, BEACHWOOD, OH, 44122"
        validate_geocoded_frame(frame, "OH")


class EffectiveBranchTests(unittest.TestCase):
    def setUp(self):
        self.sec = pd.DataFrame([
            {"advisor_crd": "5630335", "firm_crd": "105644",
             "branch_street1": "4401 KALANI DR", "branch_street2": None,
             "branch_city": "DIAMONDHEAD", "branch_state": "IL",
             "branch_postal": "60173-2096", "branch_country": "United States"},
            {"advisor_crd": "2811870", "firm_crd": "116140",
             "branch_street1": "949 WHITEWATER AVENUE", "branch_street2": None,
             "branch_city": "ST CHARLES", "branch_state": "IL",
             "branch_postal": "55972", "branch_country": "United States"},
        ])
        self.roster = pd.DataFrame([{
            "advisor_crd": "5630335", "address": "4401 KALANI DRIVE",
            "address2": "", "city": "DIAMONDHEAD", "state": "MS",
            "postal": "39525",
        }])
        self.reference = pd.DataFrame([{
            "advisor_crd": "2811870", "firm_crd": "116140",
            "old_street1": "949 WHITEWATER AVENUE", "old_city": "ST CHARLES",
            "old_state": "IL", "old_postal": "55972",
            "new_street1": "949 WHITEWATER AVENUE", "new_street2": "",
            "new_city": "ST CHARLES", "new_state": "MN", "new_postal": "55972",
            "new_country": "United States", "source": "census_exact_street_zip",
            "verified_on": "2026-08-30",
        }])

    def test_effective_layer_moves_exact_evidence_before_partitioning(self):
        effective, report = branch_geography.build_effective_branches(
            self.sec, roster=self.roster, corrections=self.reference,
            roster_source="cetera_fixture.csv")
        states = dict(zip(effective.advisor_crd, effective.branch_state))
        self.assertEqual({"5630335": "MS", "2811870": "MN"}, states)
        britt = effective[effective.advisor_crd.eq("5630335")].iloc[0]
        self.assertEqual("IL", britt.filed_branch_state)
        self.assertEqual("60173-2096", britt.filed_branch_postal)
        self.assertEqual("39525", britt.branch_postal)
        self.assertIn("firm_roster_exact_crd", britt.branch_address_source)
        self.assertEqual(2, len(report))

    def test_wrong_firm_or_name_only_roster_has_no_authority(self):
        sec = self.sec.iloc[[0]].copy()
        sec.loc[:, "firm_crd"] = "999"
        effective, report = branch_geography.apply_cetera_roster_corrections(
            sec, self.roster, "fixture.csv")
        self.assertEqual("IL", effective.iloc[0].branch_state)
        self.assertEqual([], report)

    def test_changed_source_invalidates_reviewed_reference(self):
        changed = self.sec.iloc[[1]].copy()
        changed.loc[:, "branch_postal"] = "99999"
        with self.assertRaisesRegex(branch_geography.GeographyCorrectionError,
                                    "matched 0 source rows"):
            branch_geography.apply_reference_overrides(changed, self.reference)

    def test_missing_required_reference_fails_closed(self):
        missing = ROOT / "data" / "reference" / "__missing_geography_fixture.csv"
        with mock.patch.object(branch_geography, "REFERENCE", missing):
            with self.assertRaisesRegex(
                    branch_geography.GeographyCorrectionError,
                    "required reviewed geography reference is missing"):
                branch_geography.apply_reference_overrides(self.sec)

    def test_invalid_reference_provenance_fails_closed(self):
        cases = {
            "source": "",
            "verified_on": "",
            "new_state": "ZZ",
            "new_postal": "",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                bad = self.reference.copy()
                bad.loc[0, field] = value
                with self.assertRaises(branch_geography.GeographyCorrectionError):
                    branch_geography.apply_reference_overrides(self.sec, bad)

    def test_local_repair_moves_only_when_existing_coordinate_names_new_state(self):
        effective, _ = branch_geography.build_effective_branches(
            self.sec, roster=self.roster, corrections=self.reference,
            roster_source="cetera_fixture.csv")
        old = self.sec.copy()
        old["addr_key"] = ["britt-old", "fred-old"]
        old["lat"] = [30.375, 43.969]
        old["lon"] = [-89.375, -92.065]
        old["matched"] = [
            "4401 KALANI DR, DIAMONDHEAD, MS, 39525",
            "949 WHITEWATER AVE, SAINT CHARLES, MN, 55972",
        ]
        empty = old.iloc[0:0].copy()
        moved, report = branch_geography.relocate_geocoded_frames(
            {"IL": old, "MS": empty.copy(), "MN": empty.copy()}, effective)
        self.assertTrue(moved["IL"].empty)
        self.assertEqual(["5630335"], moved["MS"].advisor_crd.tolist())
        self.assertEqual(["2811870"], moved["MN"].advisor_crd.tolist())
        self.assertEqual(2, len(report))
        self.assertEqual("MS", moved["MS"].iloc[0].branch_state)

        moved_again, second_report = branch_geography.relocate_geocoded_frames(
            moved, effective)
        self.assertEqual(["already_applied", "already_applied"],
                         [row["status"] for row in second_report])
        self.assertEqual(1, len(moved_again["MS"]))
        self.assertEqual(1, len(moved_again["MN"]))

        old.loc[old.advisor_crd.eq("5630335"), "matched"] = (
            "4401 KALANI DR, DIAMONDHEAD, IL, 39525")
        with self.assertRaisesRegex(branch_geography.GeographyCorrectionError,
                                    "existing geocoder evidence says IL"):
            branch_geography.relocate_geocoded_frames(
                {"IL": old, "MS": empty.copy(), "MN": empty.copy()}, effective)


class VerifiedOverrideTests(unittest.TestCase):
    def test_only_verified_cross_state_result_is_replaceable(self):
        frame = pd.DataFrame([
            {"lat": 30.1, "lon": -93.7,
             "matched": "100 W PARK AVE, ORANGE, TX, 77630"},
            {"lat": 41.45, "lon": -81.48,
             "matched": "100 PARK AVE, BEACHWOOD, OH, 44122"},
            {"lat": None, "lon": None, "matched": ""},
        ])
        same = pd.Series([True, True, True])
        missing, replace = apply_overrides.override_candidates(
            frame, same, "OH", verified=True)
        self.assertEqual([False, False, True], missing.tolist())
        self.assertEqual([True, False, False], replace.tolist())
        _, unverified = apply_overrides.override_candidates(
            frame, same, "OH", verified=False)
        self.assertFalse(unverified.any())

    def test_replacement_evidence_must_name_target_state(self):
        base = pd.Series({
            "source": "google_maps_manual", "verified_on": "2026-08-30",
            "check": "OK", "matched_name": "100 PARK AVE, ORANGE, TX, 77630",
        })
        self.assertFalse(apply_overrides.replacement_is_verified(base, "OH"))
        base["matched_name"] = "100 PARK AVE, BEACHWOOD, OH 44122, USA"
        self.assertTrue(apply_overrides.replacement_is_verified(base, "OH"))

    def test_empty_override_report_is_repeatable(self):
        self.assertEqual("No eligible override changes.",
                         apply_overrides.format_report([]))


if __name__ == "__main__":
    unittest.main()
