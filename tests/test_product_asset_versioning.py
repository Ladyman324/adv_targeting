"""Product helper changes must invalidate both shells, without touching data."""
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import web_assets


class ProductAssetVersionTests(unittest.TestCase):
    def test_shared_product_helper_is_hashed_and_stamped_for_both_shells(self):
        helper = "asset-product-filters.js"
        self.assertIn(helper, web_assets.VERSIONED)
        self.assertIn(helper, web_assets.FIELD_VERSIONED)
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name in set(web_assets.VERSIONED + web_assets.FIELD_VERSIONED):
                (root / name).write_text("initial shell", encoding="utf-8")
            (root / "sw.js").write_text('const VERSION = "old";\n', encoding="utf-8")
            for filename, names in (("index.html", web_assets.VERSIONED), ("field.html", web_assets.FIELD_VERSIONED)):
                (root / filename).write_text("\n".join('<script src="' + n + '?v=old"></script>' for n in names), encoding="utf-8")
            with mock.patch.object(web_assets, "WEB", root):
                before = web_assets.asset_tag(), web_assets.field_tag()
                (root / helper).write_text("changed products", encoding="utf-8")
                after = web_assets.asset_tag(), web_assets.field_tag()
                self.assertNotEqual(before[0], after[0])
                self.assertNotEqual(before[1], after[1])
                web_assets.stamp_assets()
                web_assets.stamp_field()
                self.assertEqual(web_assets.check_assets(), [])
                self.assertIn(helper + "?v=" + after[0], (root / "index.html").read_text(encoding="utf-8"))
                self.assertIn(helper + "?v=" + after[1], (root / "field.html").read_text(encoding="utf-8"))

    def test_field_worker_only_caches_helper_as_public_shell_not_data(self):
        source = (pathlib.Path(__file__).resolve().parents[1] / "webapp/sw.js").read_text(encoding="utf-8")
        self.assertIn('"asset-product-filters.js?v=" + VERSION', source)
        self.assertIn('"asset-product-filters.js", "manifest.webmanifest"', source)
        self.assertIn('if (url.pathname.startsWith("/data/")) return;', source)


if __name__ == "__main__":
    unittest.main()
