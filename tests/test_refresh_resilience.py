"""Historical pilot failure injection; no network, cloud, emails, or live writes."""
import csv
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import refresh_core as core
import refresh_resilience as resilience
import refresh_acquire as acquire


FIELDS = ["fid", "faName", "faUrl", "phone", "faCity", "faState", "email", "address", "email_status"]


def records(count=100):
    return [{"fid": str(i), "faName": "Person " + str(i),
             "faUrl": "/us-en/financial-advisor/person-" + str(i),
             "phone": "404555" + str(i).zfill(4), "faCity": "Atlanta",
             "faState": "GA" if i < 90 else "FL", "email": "person" + str(i) + "@example.com",
             "address": str(i) + " Main Street", "email_status": "ok"} for i in range(count)]


def write(path, rows, fields=FIELDS):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
    return path


class HistoricalPilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline = write(self.root / "old.csv", records())
        self.candidate = write(self.root / "new.csv", records())
        self.before = self.baseline.read_bytes()

    def assess(self, rows):
        write(self.candidate, rows)
        result = resilience.compare_edward_jones(self.candidate, self.baseline)
        self.assertEqual(self.baseline.read_bytes(), self.before)
        return result

    def codes(self, rows):
        result = self.assess(rows)
        self.assertEqual(result["status"], "held")
        return {item["code"] for item in result["issues"]}

    def test_unchanged_and_reordered_are_accepted(self):
        for rows in (records(), list(reversed(records()))):
            result = self.assess(rows)
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(result["unchangedPeople"], 100)

    def test_typical_small_changes_and_departure_are_accepted(self):
        rows = records()[1:]
        rows[0]["phone"] = "4045559999"
        rows[1]["email"] = "new.person@example.com"
        result = self.assess(rows)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["continuity"]["removed"], 1)

    def test_missing_or_duplicate_headers_are_held(self):
        for fields in (FIELDS[1:], FIELDS + ["email"]):
            write(self.candidate, records(), fields)
            result = resilience.compare_edward_jones(self.candidate, self.baseline)
            self.assertEqual(result["issues"][0]["code"], "CSV_FORMAT_INVALID")

    def test_broken_csv_and_partial_last_record_are_held(self):
        for suffix in ('"unterminated', '999,Truncated\n'):
            write(self.candidate, records())
            with self.candidate.open("a", encoding="utf-8") as handle:
                handle.write(suffix)
            result = resilience.compare_edward_jones(self.candidate, self.baseline)
            self.assertEqual(result["status"], "held")
            self.assertEqual(result["issues"][0]["code"], "CSV_FORMAT_INVALID")

    def test_valid_looking_partial_download_is_held(self):
        self.assertIn("PEOPLE_DISAPPEARED", self.codes(records()[:50]))

    def test_same_row_count_cannot_hide_removed_people(self):
        rows = records()
        for row in rows[:20]: row["fid"] = "replacement-" + row["fid"]
        self.assertIn("PEOPLE_DISAPPEARED", self.codes(rows))

    def test_same_count_field_loss_and_scrambled_values_are_held(self):
        for field in ("phone", "email", "address", "faName", "faUrl"):
            rows = records()
            for row in rows[:30]: row[field] = ""
            self.assertIn("FIELD_VALUES_LOST", self.codes(rows))
        rows = records()
        for row in rows: row["phone"] = "4045550000"
        self.assertIn("FIELD_VALUES_CHANGED", self.codes(rows))

    def test_new_shared_and_swapped_emails_are_held(self):
        rows = records(); rows[0]["email"], rows[1]["email"] = rows[1]["email"], rows[0]["email"]
        self.assertIn("EMAIL_PERSON_ASSOCIATION_CHANGED", self.codes(rows))
        rows = records(); rows[1]["email"] = rows[0]["email"]
        self.assertIn("NEW_SHARED_EMAIL_ASSOCIATIONS", self.codes(rows))

    def test_existing_shared_addresses_do_not_appear_as_new_reassignments(self):
        rows = records(); rows[1]["email"] = rows[0]["email"]
        write(self.baseline, rows); self.before = self.baseline.read_bytes()
        self.assertEqual(self.assess(rows)["status"], "accepted")

    def test_duplicates_missing_ids_and_growth_are_held(self):
        rows = records(); rows[0]["fid"] = ""
        self.assertIn("PERSON_IDS_INVALID", self.codes(rows))
        rows = records(); rows[0]["fid"] = rows[1]["fid"]
        self.assertIn("PERSON_IDS_INVALID", self.codes(rows))
        self.assertIn("UNEXPECTED_PEOPLE_GROWTH", self.codes(records(150)))

    def test_localized_state_loss_is_not_hidden_by_national_total(self):
        rows = records()
        for row in rows[90:]: row["faState"] = "GA"
        self.assertIn("STATE_COVERAGE_LOST", self.codes(rows))

    def test_http_and_unrecognized_profile_failures_are_held(self):
        for status in ("http_error", "request_error", "invalid_html", "not_processed", "new_failure"):
            rows = records(); rows[0]["email_status"] = status
            self.assertIn("PROFILE_ACQUISITION_INCOMPLETE", self.codes(rows))

    def test_no_baseline_requires_review(self):
        result = resilience.compare_edward_jones(self.candidate)
        self.assertEqual(result["status"], "held")
        self.assertIn("BASELINE_REVIEW_REQUIRED", {i["code"] for i in result["issues"]})

    def test_report_contains_no_contact_pii(self):
        result = self.assess(records())
        import json
        text = json.dumps(result)
        self.assertNotIn("person0@example.com", text)
        self.assertNotIn("Person 0", text)
        self.assertNotIn(str(self.root), text)

    def test_gate_preserves_last_good_and_keeps_rejected_bytes_private(self):
        stage = self.root / "stage"
        old = write(stage / "data/raw/firm_rosters/edward_jones_2026-09-22.csv", records())
        candidate = write(self.root / "incoming/edward_jones_2026-10-23.csv", records()[:50])
        before = old.read_bytes()
        with self.assertRaisesRegex(core.RefreshError, "held candidate"):
            acquire._prepare(stage, "roster:edward_jones", candidate, self.root, None, None, (),
                             "Reviewed aggregate coverage override")
        self.assertEqual(old.read_bytes(), before)
        self.assertFalse((old.parent / candidate.name).exists())
        reports = list((stage / "data/refresh/acquisition_reviews").rglob("comparison.json"))
        self.assertEqual(len(reports), 1)
        self.assertEqual(core.read_json(reports[0])["status"], "held")
        self.assertEqual((reports[0].parent / "held.csv").read_bytes(), candidate.read_bytes())

    def test_accepted_candidate_carries_hash_bound_comparison(self):
        stage = self.root / "stage"
        old = write(stage / "data/raw/firm_rosters/edward_jones_2026-09-22.csv", records())
        candidate = write(self.root / "incoming/edward_jones_2026-10-23.csv", records())
        products, details = acquire._prepare(stage, "roster:edward_jones", candidate, self.root,
                                             None, None, (), None)
        proof = details["historicalAcceptance"]
        self.assertEqual(proof["status"], "accepted")
        self.assertEqual(proof["candidateSha256"], core.file_hash(candidate))
        self.assertEqual(proof["baselineSha256"], core.file_hash(old))
        self.assertEqual(len(products), 1)

    def test_registration_rejects_changed_candidate_or_baseline_after_assessment(self):
        for changed in ("candidate", "baseline"):
            stage = self.root / changed
            old = write(stage / "data/raw/firm_rosters/edward_jones_2026-09-22.csv", records())
            candidate = write(self.root / changed / "incoming/edward_jones_2026-10-23.csv", records())
            products, details = acquire._prepare(stage, "roster:edward_jones", candidate,
                                                 self.root, None, None, (), None)
            target = candidate if changed == "candidate" else old
            with target.open("a", encoding="utf-8") as handle: handle.write("\n")
            with self.assertRaisesRegex(core.RefreshError, "no longer binds"):
                acquire._register(stage, {}, "roster:edward_jones", products, details, None, None, ())
            self.assertFalse((old.parent / candidate.name).exists())

    def test_complete_import_failure_then_recovery_preserves_live_baseline(self):
        # Exercise the real controller, receipts and fallback requirement, not
        # only the pure comparator. All files live in a disposable repository.
        from test_refresh_acquire import pair
        root, stage = self.root / "repo", self.root / "stage"
        root.mkdir(); stage.mkdir()
        pair(stage / "data/raw")
        old = write(stage / "data/raw/firm_rosters/edward_jones_2026-09-22.csv", records())
        original = old.read_bytes()
        directory = core.run_path(root, "pilot"); directory.mkdir(parents=True)
        core.atomic_json(stage / ".refresh-workspace.json", {"runId": "pilot", "ownerToken": "token"})
        manifest = {"schemaVersion": 1, "id": "pilot", "ownerToken": "token",
                    "workspaceRoot": str(stage), "baselineRoot": str(root), "mode": "selected-firm",
                    "firms": ["edward_jones"], "codeFiles": {}, "codeNormalization": {},
                    "inputs": core.facts(core.source_paths(stage), stage), "imports": [], "attempts": [],
                    "steps": {}, "warnings": [], "status": "ready"}
        core.save_run(directory, manifest)
        source = write(self.root / "incoming/edward_jones_2026-10-23.csv", records()[:50])
        with self.assertRaisesRegex(core.RefreshError, "held candidate"):
            acquire.import_snapshot(root, "pilot", "roster:edward_jones", source)
        failed = core.read_json(directory / "run.json")
        self.assertEqual(failed["attempts"][-1]["status"], "failed")
        self.assertEqual(failed["imports"], [])
        self.assertEqual(old.read_bytes(), original)
        self.assertFalse((root / "data/raw").exists())
        self.assertFalse((old.parent / source.name).exists())
        core.check_inputs(stage, failed)
        with self.assertRaisesRegex(core.RefreshError, "fallback-reason"):
            core.build(root, "pilot")
        write(source, records())
        recovered = acquire.import_snapshot(root, "pilot", "roster:edward_jones", source)
        self.assertEqual(recovered["attempts"][-1]["status"], "success")
        self.assertEqual(recovered["imports"][-1]["historicalAcceptance"]["status"], "accepted")
        self.assertEqual((old.parent / source.name).read_bytes(), original)
        self.assertFalse((root / "data/raw").exists())
        core.check_inputs(stage, recovered)


if __name__ == "__main__":
    unittest.main()
