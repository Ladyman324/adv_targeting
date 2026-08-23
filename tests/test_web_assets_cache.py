from __future__ import annotations

import json
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import web_assets  # noqa: E402


class DataFingerprintTests(unittest.TestCase):
    def write(self, root: pathlib.Path, name: str, value) -> None:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

    def test_fingerprint_is_stable_and_changes_for_path_or_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self.write(root, "metadata.json", {"generated_utc": "2026-08-22T03:46:41+00:00"})
            self.write(root, "tiles/b.json", {"v": 2})
            self.write(root, "tiles/a.json", {"v": 1})
            first = web_assets.data_fingerprint(root)
            self.assertRegex(first, r"^20260822T034641Z-[0-9a-f]{16}$")
            self.assertEqual(first, web_assets.data_fingerprint(root))
            self.write(root, "tiles/a.json", {"v": 3})
            self.assertNotEqual(first, web_assets.data_fingerprint(root))
            changed_content = web_assets.data_fingerprint(root)
            (root / "tiles/a.json").rename(root / "tiles/c.json")
            self.assertNotEqual(changed_content, web_assets.data_fingerprint(root))

    def test_sidecars_and_dot_manifests_are_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self.write(root, "metadata.json", {"generated_utc": "2026-08-22T03:46:41Z"})
            self.write(root, "pins_GA.json", {"pins": []})
            first = web_assets.data_fingerprint(root)
            self.write(root, ".gzip_manifest.json", {"changed": True})
            (root / "pins_GA.json.gz").write_bytes(b"derived")
            (root / "pins_GA.json.tmp").write_bytes(b"temporary")
            self.assertEqual(first, web_assets.data_fingerprint(root))

    def test_legacy_contacts_monolith_is_excluded_but_deployed_shards_are_not(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self.write(root, "metadata.json", {"generated_utc": "2026-08-22T03:46:41Z"})
            self.write(root, "contacts.json", {"legacy": 1})
            self.write(root, "contacts_base.json", {"base": 1})
            self.write(root, "contacts_0.json", {"shard": 1})
            first = web_assets.data_fingerprint(root)
            self.write(root, "contacts.json", {"legacy": 2})
            self.assertEqual(first, web_assets.data_fingerprint(root))
            self.write(root, "contacts_base.json", {"base": 2})
            changed_base = web_assets.data_fingerprint(root)
            self.assertNotEqual(first, changed_base)
            self.write(root, "contacts_0.json", {"shard": 2})
            self.assertNotEqual(changed_base, web_assets.data_fingerprint(root))


    def test_missing_or_naive_metadata_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            with self.assertRaisesRegex(ValueError, "metadata.json is missing"):
                web_assets.data_fingerprint(root)
            self.write(root, "metadata.json", {"generated_utc": "2026-08-22T03:46:41"})
            with self.assertRaisesRegex(ValueError, "timezone"):
                web_assets.data_fingerprint(root)

    def test_stamp_is_shared_and_check_detects_drift_or_missing_constant(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            app, field = root / "app.js", root / "field.js"
            app.write_text('const DATA_VERSION = "old";\n', encoding="utf-8")
            field.write_text('const DATA_VERSION = "old";\n', encoding="utf-8")
            expected = "20260822T034641Z-0123456789abcdef"
            with mock.patch.object(web_assets, "DATA_VERSION_FILES", (app, field)), \
                 mock.patch.object(web_assets, "data_fingerprint", return_value=expected):
                self.assertEqual(expected, web_assets.sync_data_version())
                self.assertIsNone(web_assets.sync_data_version())
                self.assertEqual([], web_assets.check_data_versions())
                field.write_text("// missing\n", encoding="utf-8")
                self.assertIn("field.js has no DATA_VERSION", web_assets.check_data_versions())
                app.write_text('const DATA_VERSION = "before";\n', encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "missing DATA_VERSION in: field.js"):
                    web_assets.sync_data_version()
                self.assertIn('DATA_VERSION = "before"', app.read_text(encoding="utf-8"))


    def test_check_recomputes_and_detects_post_stamp_data_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            app, field = root / "app.js", root / "field.js"
            self.write(root, "metadata.json", {"generated_utc": "2026-08-22T03:46:41Z"})
            self.write(root, "pins_GA.json", {"pins": [1]})
            real_fingerprint = web_assets.data_fingerprint
            stamped = real_fingerprint(root)
            app.write_text(f'const DATA_VERSION = "{stamped}";\n', encoding="utf-8")
            field.write_text(f'const DATA_VERSION = "{stamped}";\n', encoding="utf-8")
            with mock.patch.object(web_assets, "DATA_VERSION_FILES", (app, field)), \
                 mock.patch.object(web_assets, "data_fingerprint",
                                   side_effect=lambda: real_fingerprint(root)):
                self.assertEqual([], web_assets.check_data_versions())
                self.write(root, "pins_GA.json", {"pins": [1, 2]})
                problems = web_assets.check_data_versions()
                self.assertEqual(2, len(problems))
                self.assertTrue(all("!= data fingerprint" in p for p in problems))

    def test_main_final_check_does_not_reuse_pre_refresh_fingerprint(self):
        with mock.patch.object(sys, "argv", ["web_assets.py"]), \
             mock.patch.object(web_assets, "sync_data_version", return_value="stamped"), \
             mock.patch.object(web_assets, "stamp_assets", return_value=False), \
             mock.patch.object(web_assets, "stamp_field", return_value=False), \
             mock.patch.object(web_assets, "refresh_all"), \
             mock.patch.object(web_assets, "check", return_value=0) as final_check:
            web_assets.main()
        final_check.assert_called_once_with()

    def test_main_exits_nonzero_when_final_check_finds_post_refresh_drift(self):
        with mock.patch.object(sys, "argv", ["web_assets.py"]), \
             mock.patch.object(web_assets, "sync_data_version", return_value=None), \
             mock.patch.object(web_assets, "stamp_assets", return_value=False), \
             mock.patch.object(web_assets, "stamp_field", return_value=False), \
             mock.patch.object(web_assets, "refresh_all"), \
             mock.patch.object(web_assets, "check", return_value=2) as final_check:
            with self.assertRaises(SystemExit) as stopped:
                web_assets.main()
        self.assertEqual(2, stopped.exception.code)
        final_check.assert_called_once_with()




class CachePolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(
            (ROOT / "webapp" / "staticwebapp.config.json").read_text(encoding="utf-8"))
        self.routes = self.config["routes"]

    def test_short_private_cache_precedes_no_cache_fallback(self):
        names = [route["route"] for route in self.routes]
        fast = ["/data/pins_*.json", "/data/national_view.json",
                "/data/offices_national.json"]
        for name in fast:
            route = next(route for route in self.routes if route["route"] == name)
            self.assertEqual(["authenticated"], route["allowedRoles"])
            self.assertEqual("private, max-age=300, must-revalidate",
                             route["headers"]["Cache-Control"])
            self.assertLess(names.index(name), names.index("/data/*"))
        positive_ttl = set()
        for route in self.routes:
            if not route["route"].startswith("/data"):
                continue
            header = route.get("headers", {}).get("Cache-Control", "")
            match = re.search(r"(?:^|,)\s*max-age=(\d+)", header)
            if match and int(match.group(1)) > 0:
                positive_ttl.add(route["route"])
        self.assertEqual(set(fast), positive_ttl)
        fallback = next(route for route in self.routes if route["route"] == "/data/*")
        self.assertEqual(["authenticated"], fallback["allowedRoles"])
        self.assertEqual("no-cache", fallback["headers"]["Cache-Control"])
        api = next(route for route in self.routes if route["route"] == "/api/*")
        self.assertEqual("no-store", api["headers"]["Cache-Control"])


    def test_desktop_has_no_raw_generated_data_fetches(self):
        source = (ROOT / "webapp" / "app.js").read_text(encoding="utf-8")
        for caller in ("fetch", "grab"):
            for quote in (chr(96), chr(34), chr(39)):
                self.assertNotIn(f"{caller}({quote}data/", source)

    def test_field_has_no_raw_generated_data_fetches(self):
        source = (ROOT / "webapp" / "field.js").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(?:fetch|grab)\(\s*[`\"']data/", source))
        expected = ["act_assets.json", "territories.json", "tile_index.json",
                    "tiles/${k}.json", "practices/${key}.json", "name_index.json",
                    "names/${key}.json", "geo_index.json"]
        for path in expected:
            self.assertIn(f'dataUrl(`{path}`)' if "${" in path else f'dataUrl("{path}")', source)
        self.assertIn('path.includes("?") ? "&" : "?"', source)

    def test_service_worker_still_excludes_data_from_cache_storage(self):
        source = (ROOT / "webapp" / "sw.js").read_text(encoding="utf-8")
        self.assertRegex(source, r'if \(url\.pathname\.startsWith\("/data/"\)\) return;')


if __name__ == "__main__":
    unittest.main()
