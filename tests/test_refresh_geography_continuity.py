"""Keep exact personal geocode evidence without choosing between uncertain points."""
from pathlib import Path
import sys
import tempfile
import unittest

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from refresh_geography import cached_geography


class PersonGeocodeContinuityTests(unittest.TestCase):
    def row(self, advisor="1", firm="10", **changes):
        return {
            "advisor_crd": advisor, "firm_crd": firm,
            "branch_street1": "100 MARKET ST", "branch_street2": "SUITE 1",
            "branch_city": "SAN FRANCISCO", "branch_state": "CA",
            "branch_postal": "94105", "branch_country": "United States",
            "lat": 37.77, "lon": -122.41, "geocode_precision": "street",
            "matched": "100 MARKET ST, SAN FRANCISCO, CA, 94105",
            "geocode_source": "census", **changes,
        }

    def rebuild(self, current, cached):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, interim = root / "data/output", root / "data/interim"
            output.mkdir(parents=True)
            interim.mkdir(parents=True)
            coordinates = {"lat", "lon", "matched", "geocode_precision", "geocode_source"}
            branch = {k: v for k, v in current.items() if k not in coordinates}
            pd.DataFrame([branch]).to_parquet(output / "advisor_branches.parquet", index=False)
            pd.DataFrame([{"advisor_crd": current["advisor_crd"], "first_name": "Person", "last_name": "Example"}]).to_parquet(output / "advisors.parquet", index=False)
            pd.DataFrame([{"crd": current["firm_crd"], "firm_display": "Firm", "opportunity_score": 1.0, "motion": "sma_led"}]).to_parquet(output / "firms.parquet", index=False)
            pd.DataFrame(cached).to_parquet(interim / "branch_geocoded_CA.parquet", index=False)
            cached_geography(root)
            return pd.read_parquet(interim / "branch_geocoded_CA.parquet").iloc[0]

    def test_own_exact_point_survives_another_persons_conflicting_cache(self):
        current = self.row()
        other = self.row(advisor="2", lat=37.78, lon=-122.42)
        row = self.rebuild(current, [current, other])
        self.assertEqual((row.lat, row.lon), (37.77, -122.41))
        self.assertEqual(row.geocode_precision, "street")
        self.assertEqual(row.geocode_source, "census")

    def test_conflicting_own_points_stay_unresolved(self):
        row = self.rebuild(self.row(), [self.row(), self.row(lat=37.78, lon=-122.42)])
        self.assertTrue(pd.isna(row.lat))

    def test_an_old_point_is_not_followed_to_a_new_street(self):
        current = self.row(branch_street1="200 MARKET ST")
        row = self.rebuild(current, [self.row(), self.row(advisor="2", lat=37.78, lon=-122.42)])
        self.assertTrue(pd.isna(row.lat))

    def test_a_different_firm_does_not_break_a_shared_address_tie(self):
        current = self.row(firm="30")
        row = self.rebuild(current, [self.row(), self.row(advisor="2", lat=37.78, lon=-122.42)])
        self.assertTrue(pd.isna(row.lat))

    def test_an_empty_street_does_not_create_personal_evidence(self):
        current = self.row(branch_street1="")
        other = self.row(advisor="2", branch_street1="", lat=37.78, lon=-122.42)
        row = self.rebuild(current, [current, other])
        self.assertTrue(pd.isna(row.lat))

    def test_existing_unambiguous_address_fallback_is_preserved(self):
        row = self.rebuild(self.row(advisor="3", firm="30"), [self.row()])
        self.assertEqual((row.lat, row.lon), (37.77, -122.41))

    def test_full_unit_address_is_required_for_personal_reuse(self):
        current = self.row(branch_street2="SUITE 2")
        other = self.row(advisor="2", branch_street2="SUITE 2", lat=37.78, lon=-122.42)
        row = self.rebuild(current, [self.row(), other])
        self.assertEqual((row.lat, row.lon), (37.78, -122.42))


if __name__ == "__main__":
    unittest.main()
