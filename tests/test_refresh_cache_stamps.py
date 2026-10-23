"""Actual cache-stamp mutations preserve integrity-pinned source bytes."""
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import refresh_core as core
import web_assets


class CacheStampByteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.web = Path(self.temp.name) / "webapp"
        self.web.mkdir()
        self.expected_version = "20261023T120000Z-0123456789abcdef"
        self.shells = ("app.js", "field.js", "sw.js", "index.html", "field.html")

    def fixture(self, endings):
        for name in set(web_assets.VERSIONED) | set(web_assets.FIELD_VERSIONED):
            (self.web / name).write_bytes(b"/* unchanged dependency */\n")
        rows = {
            "app.js": ['// keep caf\u00e9', 'const DATA_VERSION = "old";', 'const keep = "desktop";', "// tail"],
            "field.js": ['// keep caf\u00e9', 'const DATA_VERSION = "old";', 'const keep = "field";', "// tail"],
            "sw.js": ['// keep caf\u00e9', 'const VERSION = "old";', 'const keep = "worker";', "// tail"],
            "index.html": ["<!-- keep caf\u00e9 -->"] + [
                '<script src="' + name + '?v=old"></script>' for name in web_assets.VERSIONED] + ["<!-- tail -->"],
            "field.html": ["<!-- keep caf\u00e9 -->"] + [
                '<script src="' + name + '?v=old"></script>' for name in web_assets.FIELD_VERSIONED] + ["<!-- tail -->"],
        }
        for name, lines in rows.items():
            # No final newline, alongside mixed internal endings and UTF-8,
            # makes newline translation and incidental append visible.
            raw = b"".join(line.encode("utf-8") + (endings[index % len(endings)] if index < len(lines) - 1 else b"")
                           for index, line in enumerate(lines))
            (self.web / name).write_bytes(raw)
        return {name: (self.web / name).read_bytes() for name in self.shells}

    def environment(self):
        return (patch.object(web_assets, "WEB", self.web),
                patch.object(web_assets, "DATA_VERSION_FILES", (self.web / "app.js", self.web / "field.js")),
                patch.object(web_assets, "data_fingerprint", return_value=self.expected_version))

    def stamp(self):
        self.assertEqual(web_assets.sync_data_version(), self.expected_version)
        desktop_tag = web_assets.asset_tag()
        field_tag = web_assets.field_tag()
        self.assertTrue(web_assets.stamp_assets())
        self.assertTrue(web_assets.stamp_field())
        self.assertEqual(web_assets.check_assets(), [])
        self.assertEqual(web_assets.check_data_versions(), [])
        return desktop_tag, field_tag

    def expected(self, name, before, desktop_tag, field_tag):
        if name in {"app.js", "field.js"}:
            return before.replace(b'const DATA_VERSION = "old";',
                                  ('const DATA_VERSION = "' + self.expected_version + '";').encode("ascii"))
        if name == "sw.js":
            return before.replace(b'const VERSION = "old";', ('const VERSION = "' + field_tag + '";').encode("ascii"))
        names = web_assets.VERSIONED if name == "index.html" else web_assets.FIELD_VERSIONED
        tag = desktop_tag if name == "index.html" else field_tag
        for asset in names:
            before = before.replace((asset + "?v=old").encode("ascii"), (asset + "?v=" + tag).encode("ascii"))
        return before

    def test_actual_updaters_preserve_all_nonstamp_bytes_and_hashes(self):
        for label, endings in (("LF", [b"\n"]), ("CRLF", [b"\r\n"]),
                               ("mixed", [b"\r\n", b"\n", b"\r\n", b"\n"])):
            with self.subTest(endings=label):
                before = self.fixture(endings)
                hashes = {name: core.normalized_code_hash(self.web / name, "webapp/" + name) for name in self.shells}
                web, versions, fingerprint = self.environment()
                with web, versions, fingerprint:
                    desktop_tag, field_tag = self.stamp()
                for name in self.shells:
                    with self.subTest(file=name):
                        after = (self.web / name).read_bytes()
                        self.assertEqual(after, self.expected(name, before[name], desktop_tag, field_tag))
                        self.assertEqual(re.findall(rb"\r\n|\r|\n", after), re.findall(rb"\r\n|\r|\n", before[name]))
                        self.assertEqual(core.normalized_code_hash(self.web / name, "webapp/" + name), hashes[name])

    def test_unchanged_rerun_is_byte_stable(self):
        self.fixture([b"\r\n", b"\n"])
        web, versions, fingerprint = self.environment()
        with web, versions, fingerprint:
            self.stamp()
            before = {name: (self.web / name).read_bytes() for name in self.shells}
            self.assertIsNone(web_assets.sync_data_version())
            self.assertFalse(web_assets.stamp_assets())
            self.assertFalse(web_assets.stamp_field())
            self.assertEqual(web_assets.check_assets(), [])
            self.assertEqual(before, {name: (self.web / name).read_bytes() for name in self.shells})

    def test_normalizer_still_rejects_real_code_and_line_ending_changes(self):
        self.fixture([b"\r\n", b"\n"])
        web, versions, fingerprint = self.environment()
        with web, versions, fingerprint:
            self.stamp()
        for name in self.shells:
            path = self.web / name
            original = path.read_bytes()
            baseline = core.normalized_code_hash(path, "webapp/" + name)
            executable = b"\nconst unexpected = true;" if name.endswith(".js") else b"\n<script>window.unexpected=true;</script>"
            for label, changed in (("real code", original + executable),
                                   ("newline only", original.replace(b"\r\n", b"\n", 1))):
                with self.subTest(file=name, edit=label):
                    path.write_bytes(changed)
                    self.assertNotEqual(core.normalized_code_hash(path, "webapp/" + name), baseline)
            path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
