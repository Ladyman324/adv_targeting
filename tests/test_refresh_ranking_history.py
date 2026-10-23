"""Same-edition continuity is different from a new edition's membership."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import refresh_core as core
import refresh_history as history
import refresh_acquire as acquire
import refresh_review as review
from test_refresh_acquire import pair


def rows(year=2025, count=100, offset=0, category="best-in-state", scope="Georgia", provider="forbes"):
    return [{("list" if provider == "forbes" else "ranking"): category,
             ("name" if provider == "forbes" else "advisor"): "Person " + str(i + offset),
             ("category" if provider == "forbes" else "state"): scope,
             ("uri" if provider == "forbes" else "crd"): str(i + offset),
             "year": year, "rank": i + 1, "firm": "Fixture firm", "city": "Atlanta"} for i in range(count)]


class RankingHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.stage = Path(self.temp.name) / "stage"; self.stage.mkdir()
        self.old = self.stage / "data/raw/forbes_rankings.json"
        core.atomic_json(self.old, rows())
        self.candidate = Path(self.temp.name) / "incoming.json"
        core.atomic_json(self.candidate, rows())

    def assess(self, records, provider="forbes", categories=()):
        core.atomic_json(self.candidate, records)
        return history.compare_rankings(self.candidate, [self.old], provider, categories)

    def codes(self, records):
        result = self.assess(records)
        self.assertEqual(result["status"], "held")
        return {item["code"] for item in result["issues"]}

    def test_same_edition_identical_or_reordered_is_accepted(self):
        for records in (rows(), list(reversed(rows()))):
            self.assertEqual(self.assess(records)["status"], "accepted")

    def test_new_edition_can_have_entirely_different_winners(self):
        result = self.assess(rows(year=2026, offset=1000))
        self.assertEqual(result["status"], "accepted")
        self.assertFalse(result["cohortComparisons"][0]["sameEdition"])

    def test_same_edition_replaced_people_or_partial_download_is_held(self):
        self.assertIn("RANKING_SAME_EDITION_PEOPLE_LOST", self.codes(rows(offset=1000)))
        self.assertIn("RANKING_SCOPE_COVERAGE_LOST", self.codes(rows(count=50)))

    def test_missing_geographic_scope_is_detected_across_editions(self):
        core.atomic_json(self.old, rows() + rows(scope="Florida", offset=1000))
        self.assertIn("RANKING_GEOGRAPHY_COVERAGE_LOST", self.codes(rows(year=2026)))

    def test_missing_category_is_not_silent_but_explicit_subset_is_allowed(self):
        core.atomic_json(self.old, rows() + rows(category="top-women", offset=1000))
        self.assertIn("RANKING_CATEGORY_COVERAGE_LOST", self.codes(rows(year=2026)))
        result = self.assess(rows(year=2026), categories=("best-in-state",))
        self.assertEqual(result["status"], "accepted")

    def test_rank_firm_and_identifier_losses_are_separate_from_count(self):
        for field in ("rank", "firm", "uri", "city"):
            records = rows(year=2026)
            for row in records: row[field] = None
            self.assertIn("RANKING_FIELD_COVERAGE_LOST", self.codes(records))

    def test_unknown_edition_is_not_relabelled_as_new_year(self):
        result = self.assess(rows(year=None))
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["candidate"]["editions"], ["unknown"])
        self.assertIn("RANKING_EDITION_UNDECLARED", {item["code"] for item in result["issues"]})

    def test_legacy_unknown_year_self_comparison_is_accepted(self):
        core.atomic_json(self.old, rows(year=None))
        self.assertEqual(self.assess(rows(year=None))["status"], "accepted")

    def test_historical_membership_is_compared_with_its_own_edition_not_future(self):
        core.atomic_json(self.old, rows(year=2025) + rows(year=2026, offset=1000))
        result = self.assess(rows(year=2025))
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["cohortComparisons"][0]["previousEdition"], 2025)

    def test_immutable_editions_are_used_when_canonical_union_is_absent(self):
        self.old.unlink()
        file = self.stage / "data/refresh/ranking_editions/forbes/prior.json"
        core.atomic_json(file, rows())
        core.atomic_json(self.candidate, rows(year=2026, count=20))
        result = history.compare_source(self.stage, "forbes", self.candidate)
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["baselineFacts"], core.facts([file], self.stage))

    def test_barrons_uses_its_native_category_state_and_crd(self):
        core.atomic_json(self.old, rows(provider="barrons", category="top1200", scope="GA"))
        result = self.assess(rows(year=2026, provider="barrons", category="top1200", scope="GA", offset=1000), provider="barrons")
        self.assertEqual(result["status"], "accepted")

    def test_new_duplicate_recognition_is_held_but_existing_duplicates_are_reported(self):
        records = rows(); records.append(dict(records[0]))
        self.assertIn("RANKING_DUPLICATES_EXPANDED", self.codes(records))
        core.atomic_json(self.old, records)
        result = self.assess(records)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["candidate"]["duplicateRecognitionRows"], 1)

    def test_new_conflicting_duplicate_cannot_hide_in_a_stable_count(self):
        records = rows(); extra = dict(records[0]); extra["firm"] = "Different firm"
        records.append(extra)
        self.assertIn("RANKING_CONFLICTING_DUPLICATES_EXPANDED", self.codes(records))

    def test_real_import_hold_review_and_accept_preserves_older_recognition(self):
        root = Path(self.temp.name) / "repo"; root.mkdir()
        pair(self.stage / "data/raw")
        core.atomic_json(self.stage / ".refresh-workspace.json", {"runId": "fixture", "ownerToken": "token"})
        directory = core.run_path(root, "fixture"); directory.mkdir(parents=True)
        value = {"schemaVersion": 1, "id": "fixture", "ownerToken": "token", "workspaceRoot": str(self.stage),
                 "baselineRoot": str(root), "mode": "rankings-only", "firms": [], "codeFiles": {}, "codeNormalization": {},
                 "inputs": core.facts(core.source_paths(self.stage, "rankings-only"), self.stage), "imports": [], "attempts": [],
                 "steps": {}, "warnings": [], "status": "ready"}
        core.save_run(directory, value)
        original = self.old.read_bytes()
        core.atomic_json(self.candidate, rows(year=2026, count=50))
        with self.assertRaisesRegex(core.RefreshError, "RANKING_SCOPE_COVERAGE_LOST"):
            acquire.import_snapshot(root, "fixture", "forbes", self.candidate, edition="2026")
        self.assertEqual(self.old.read_bytes(), original)
        self.assertFalse(list((self.stage / "data/refresh/ranking_editions/forbes").glob("*.json")))
        report = next((self.stage / "data/refresh/acquisition_reviews/forbes").glob("*/comparison.json"))
        codes = sorted({item["code"] for item in core.read_json(report)["issues"]})
        review.review(root, "fixture", report.parent.name, "approve", "Fixture operator",
                      "Verified a genuine supplier list-size reduction", codes, source_id="forbes")
        result = acquire.import_snapshot(root, "fixture", "forbes", self.candidate, edition="2026")
        self.assertEqual(result["imports"][-1]["records"], 50)
        self.assertEqual(result["imports"][-1]["historicalAcceptance"]["status"], "accepted")
        self.assertEqual(self.old.read_bytes(), original)
        self.assertFalse((root / "data/raw").exists())


if __name__ == "__main__":
    unittest.main()
