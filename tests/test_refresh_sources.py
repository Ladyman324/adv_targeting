"""Offline fixtures for source refresh integrity and history preservation."""
import gzip
import io
import json
import pathlib
import sys
import tempfile
import shutil
import subprocess
import unittest
import zipfile
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import fetch_feeds
from refresh_geography import cached_geography
from refresh_rankings import merge_rankings


class RefreshSourcesTests(unittest.TestCase):
    def test_offline_census_cache_is_byte_for_byte_read_only(self):
        import contact_work_locations as locations
        with tempfile.TemporaryDirectory() as temp:
            cache = pathlib.Path(temp) / "cache.csv"
            original = b'_loc_key,lat,lon\r\n"100 MAIN|ATLANTA|GA|30309",33.8000,-84.4000\r\n'
            cache.write_bytes(original)
            candidates = pd.DataFrame([{"branch_street1": "100 Main", "branch_city": "Atlanta",
                "branch_state": "GA", "branch_postal": "30309", "lat": float("nan"),
                "lon": float("nan"), "coordinate_origin": ""}])
            with patch.object(locations, "CACHE", cache), patch.object(locations, "_post_chunk", side_effect=AssertionError("network forbidden")):
                result = locations.free_geocode(candidates, allow_network=False)
            self.assertAlmostEqual(result.iloc[0]["lat"], 33.8)
            self.assertEqual(cache.read_bytes(), original)

    @unittest.skipUnless(shutil.which("node"), "Node is required for browser-script fixtures")
    def test_browser_harvest_configuration(self):
        subprocess.run([shutil.which("node"), str(pathlib.Path(__file__).with_name("test_ranking_harvest.js"))], check=True)

    def test_feed_integrity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "feed.gz"
            path.write_bytes(gzip.compress(b"<Root><Firm/></Root>"))
            self.assertEqual(fetch_feeds.validate_feed(path), 1)
            path.write_bytes(path.read_bytes()[:-4])
            with self.assertRaises((EOFError, OSError)):
                fetch_feeds.validate_feed(path)
            zip_path = pathlib.Path(temp) / "feed.zip"
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("part.xml", "<Root><Indvl/></Root>")
            self.assertEqual(fetch_feeds.validate_feed(zip_path, individual=True), 1)

    def test_failed_download_never_registers_final(self):
        with tempfile.TemporaryDirectory() as temp:
            class Response(io.BytesIO):
                headers = {"Content-Length": "99999"}
            with patch.object(fetch_feeds, "RAW", pathlib.Path(temp)), patch.object(
                    fetch_feeds.urllib.request, "urlopen", return_value=Response(b"partial")):
                with self.assertRaises(ValueError):
                    fetch_feeds.download("firm_sec", lookback=1)
            self.assertEqual(list(pathlib.Path(temp).iterdir()), [])

    def test_barrons_history_and_input_immutability(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            old = root / "old.json"
            new = root / "new.json"
            old.write_text(json.dumps([{"ranking": "top100", "advisor": "Old Advisor", "crd": "1", "rank": 5}]))
            new.write_text(json.dumps([{"ranking": "top100", "advisor": "New Advisor", "crd": "2", "rank": 4}]))
            checksum = old.read_bytes()
            merge_rankings(root, "barrons", [{"path": old, "edition": 2024}])
            result = merge_rankings(root, "barrons", [{"path": new, "edition": 2026, "categories": ["top100"]}])
            self.assertEqual(result["years"], [2024, 2026])
            self.assertEqual(result["rows"], 2)
            self.assertEqual(old.read_bytes(), checksum)
            result = merge_rankings(root, "barrons", [])
            self.assertEqual(result["rows"], 2)

    def test_forbes_unknown_year_and_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            source = root / "source.json"
            source.write_text(json.dumps([
                {"list": "best-in-state", "name": "One", "uri": "one", "category": "Georgia - State", "rank": 1, "year": 2024},
                {"list": "top-wealth", "name": "One", "uri": "one", "rank": 1},
                {"list": "top-wealth", "name": "One", "uri": "one", "rank": 2, "year": 2024}]))
            result = merge_rankings(root, "forbes", [source])
            self.assertEqual(result["unknownYear"], 1)
            table = pd.read_parquet(root / "data/interim/forbes_rankings.parquet")
            self.assertEqual(table.loc[table.year.eq(2024), "rank_state"].tolist(), ["GA", "GA"])
            self.assertTrue(table.loc[table.year.isna(), "rank_state"].isna().all())
            with self.assertRaises(ValueError):
                merge_rankings(root, "forbes", [{"path": source, "edition": 2026}])
            with self.assertRaises(ValueError):
                merge_rankings(root, "forbes", [{"path": source, "categories": ["missing"]}])

    def test_geography_rebuild_moves_removes_and_adds_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            interim, output = root / "data/interim", root / "data/output"
            interim.mkdir(parents=True)
            output.mkdir(parents=True)
            def branch(crd, state="GA", street="1 MAIN ST"):
                return {"advisor_crd": crd, "firm_crd": "10", "branch_street1": street,
                        "branch_street2": "", "branch_city": "CITY", "branch_state": state, "branch_postal": "12345"}
            old = pd.DataFrame([branch("1"), branch("2")]).assign(lat=33.0, lon=-84.0,
                geocode_precision="rooftop", matched="1 MAIN ST, CITY, GA, 12345", geocode_source="census")
            old.to_parquet(interim / "branch_geocoded_GA.parquet", index=False)
            pd.DataFrame([branch("1", "FL"), branch("3", "GA")]).to_parquet(output / "advisor_branches.parquet", index=False)
            pd.DataFrame([{"advisor_crd": str(n), "first_name": "First", "last_name": "Last"} for n in (1, 3)]).to_parquet(output / "advisors.parquet", index=False)
            pd.DataFrame([{"crd": "10", "firm_display": "Firm", "opportunity_score": 50.0, "motion": "SMA"}]).to_parquet(output / "firms.parquet", index=False)
            result = cached_geography(root)
            self.assertEqual(result["GA"]["placed"], 1)  # new person, same exact physical address
            self.assertEqual(result["FL"]["placed"], 0)  # moved state must never inherit old CRD point
            self.assertEqual(pd.read_parquet(interim / "branch_geocoded_GA.parquet").advisor_crd.tolist(), ["3"])
            pd.DataFrame([branch("3")]).to_parquet(output / "advisor_branches.parquet", index=False)
            cached_geography(root)
            self.assertFalse((interim / "branch_geocoded_FL.parquet").exists())
            columns = pd.read_parquet(interim / "branch_geocoded_GA.parquet").columns
            self.assertTrue({"advisor_crd", "firm_crd", "first_name", "last_name", "firm_display",
                             "opportunity_score", "motion", "lat", "lon", "matched",
                             "geocode_precision", "geocode_source", "addr_key"}.issubset(columns))

    def test_city_membership_regenerated_and_missing_cache_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            interim, output, web = root / "data/interim", root / "data/output", root / "webapp/data"
            interim.mkdir(parents=True)
            output.mkdir(parents=True)
            web.mkdir(parents=True)
            pd.DataFrame([{"advisor_crd": "3", "firm_crd": "10", "branch_street1": None,
                           "branch_street2": None, "branch_city": "CITY", "branch_state": "GA",
                           "branch_postal": None}]).to_parquet(output / "advisor_branches.parquet", index=False)
            people = pd.DataFrame([{"advisor_crd": "3", "first_name": "First", "last_name": "Last"}])
            people.to_parquet(output / "advisors.parquet", index=False)
            people.to_parquet(interim / "advisors.parquet", index=False)
            pd.DataFrame([{"crd": "10", "name": "Firm", "firm_display": "Firm", "opportunity_score": 50.0, "motion": "SMA"}]).to_parquet(output / "firms.parquet", index=False)
            pd.DataFrame([{"advisor_crd": "deleted"}]).to_parquet(interim / "branch_city_level.parquet", index=False)
            with self.assertRaisesRegex(ValueError, "city-only membership"):
                cached_geography(root)
            (web / "geo_index.json").write_text(json.dumps({"cities": {"CITY": [["GA", 33.0, -84.0]]}}))
            cached_geography(root)
            rows = pd.read_parquet(interim / "branch_city_level.parquet")
            self.assertEqual(rows.advisor_crd.tolist(), ["3"])
            self.assertTrue(rows.city_level.all())
            self.assertEqual(rows.geocode_precision.tolist(), ["city"])

    def test_field_ranking_years_legacy_and_new(self):
        import build_field_tiles
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root / "barrons.json").write_text(json.dumps({"advisors": {"1": [["top100", 1, None, 2024, ""]]}}))
            (root / "forbes.json").write_text(json.dumps({"advisors": {"1": []}, "latestYear": {"1": 2026, "2": None}}))
            with patch.object(build_field_tiles, "WEB", root):
                self.assertEqual(build_field_tiles.load_ranked_years(), {"1": 2026})
            self.assertEqual(build_field_tiles.COLUMNS[-1], "ranked_year")

    def test_export_latest_year_and_historical_assets(self):
        import export_barrons
        import export_forbes
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            raw = root / "data/raw"
            raw.mkdir(parents=True)
            source = root / "editions.json"
            source.write_text(json.dumps([
                {"ranking": "top100", "advisor": "One", "crd": "1", "rank": 1, "year": 2024},
                {"ranking": "top100", "advisor": "One", "crd": "1", "rank": 4, "year": 2026}]))
            merge_rankings(root, "barrons", [source])
            out = root / "webapp/data"
            out.mkdir(parents=True)
            (out / "advisor_index.json").write_text(json.dumps({"advisors": [["1"]]}))
            with patch.object(export_barrons, "INTERIM", root / "data/interim"), patch.object(export_barrons, "OUT", out):
                export_barrons.main()
            result = json.loads((out / "barrons.json").read_text())
            self.assertEqual(result["latestYear"]["1"], 2026)
            self.assertEqual(result["advisors"]["1"][0][3], 2026)
            rows = [{"advisor_crd": "1", "match_tier": "high", "rank": 1,
                     "category": "Georgia - State", "rank_state": "GA", "rank_market": None,
                     "rank_segment": None, "forbes_uri": "one", "team_assets_usd": 1000,
                     "year": 2024},
                    {"advisor_crd": "1", "match_tier": "high", "rank": 4,
                     "category": "Georgia - State", "rank_state": "GA", "rank_market": None,
                     "rank_segment": None, "forbes_uri": "one", "team_assets_usd": 500,
                     "year": 2026}]
            pd.DataFrame(rows).to_parquet(root / "data/interim/forbes_matches.parquet", index=False)
            with patch.object(export_forbes, "INTERIM", root / "data/interim"), patch.object(export_forbes, "OUT", out):
                export_forbes.main()
            result = json.loads((out / "forbes.json").read_text())
            self.assertEqual(result["latestYear"]["1"], 2026)
            self.assertEqual(result["advisors"]["1"][0][5], 2026)
            self.assertIn("2026", result["advisors"]["1"][0][1])
            self.assertEqual(result["team_assets"]["1"], 500)
            self.assertEqual(result["team_assets_year"]["1"], 2026)


if __name__ == "__main__":
    unittest.main()
