"""Forbes export schema stays compatible without fabricating legacy editions."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from validate_webapp_data import validate_forbes_entries


class ForbesValidationTests(unittest.TestCase):
    def test_legacy_unknown_year_preserved_and_current_year_accepted(self):
        legacy = ['TOP WEALTH #1', 'Forbes national', 1, 'h', 'https://example.com']
        entries = [legacy, legacy + [2026], legacy + [None]]
        before = copy.deepcopy(entries)
        validate_forbes_entries(entries)
        self.assertEqual(entries, before)
        self.assertEqual(len(entries[0]), 5)
        self.assertIsNone(entries[2][5])

    def test_bad_year_types_and_ranges_rejected(self):
        row = ['TOP WEALTH #1', 'Forbes national', 1, 'c', 'https://example.com']
        for year in (True, False, '2026', 2026.0, 0, 1899, 2201, [], {}):
            with self.subTest(year=year), self.assertRaisesRegex(AssertionError, 'year'):
                validate_forbes_entries([row + [year]])

    def test_only_approved_tiers_and_supported_widths(self):
        for row in ([1, 2, 3, 'review', 5], [1, 2, 3, 'none', 5, 2026],
                    [1, 2, 3, 'c'], [1, 2, 3, 'c', 5, 2026, 'extra'], None):
            with self.subTest(row=row), self.assertRaises(AssertionError):
                validate_forbes_entries([row])


if __name__ == '__main__':
    unittest.main()
