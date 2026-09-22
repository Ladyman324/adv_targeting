import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from edward_jones_async import extract_bio


class EdwardJonesBioTests(unittest.TestCase):
    def test_full_widget_bio_is_plain_text_with_paragraphs(self):
        page = """
        <html><body>
          <div data-widget="enhanced-fa-bio">
            <script type="application/json">
              {"content":"<p>First &amp; trusted&trade; paragraph.</p><p>Second<br>line.</p>"}
            </script>
          </div>
        </body></html>
        """
        self.assertEqual(
            extract_bio(page),
            "First & trusted\u2122 paragraph.\nSecond\nline.",
        )

    def test_missing_or_malformed_widget_is_not_guessed(self):
        self.assertIsNone(extract_bio("<html><body>Short visible bio</body></html>"))
        self.assertIsNone(extract_bio(
            '<div data-widget="enhanced-fa-bio">'
            '<script type="application/json">not json</script></div>'
        ))


if __name__ == "__main__":
    unittest.main()
