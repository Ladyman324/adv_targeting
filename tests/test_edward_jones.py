import asyncio
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from edward_jones_async import (
    RateLimiter,
    advisor_identity,
    extract_bio,
    validate_discovery,
)


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


class EdwardJonesDiscoveryTests(unittest.TestCase):
    def test_personal_fid_wins_over_reused_entity_id(self):
        first = {
            "faEntityId": 1924514,
            "fid": 661426,
            "faUrl": "/us-en/financial-advisor/changyong-shi",
        }
        second = {
            "faEntityId": 1924514,
            "fid": 661421,
            "faUrl": "/us-en/financial-advisor/cindy-shi",
        }
        self.assertEqual(advisor_identity(first), "fid:661426")
        self.assertEqual(len(validate_discovery([first, second], expected=2)), 2)

    def test_profile_url_is_identity_fallback_before_entity_id(self):
        row = {
            "faEntityId": 123,
            "faUrl": "/us-en/financial-advisor/example",
        }
        self.assertEqual(
            advisor_identity(row),
            "faUrl:/us-en/financial-advisor/example",
        )

    def test_rate_limiter_stays_slow_after_source_throttle(self):
        async def exercise():
            limiter = RateLimiter(2.0)
            await limiter.cap_rate(1.0)
            first = limiter.interval
            await limiter.cap_rate(2.0)
            return first, limiter.interval

        self.assertEqual(asyncio.run(exercise()), (1.0, 1.0))


if __name__ == "__main__":
    unittest.main()
