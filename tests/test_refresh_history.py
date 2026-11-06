"""Provider-aware historical comparisons do not perform CRD/ACT matching."""
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import refresh_core as core
import refresh_history as history


def rows(count=100):
    return [{"advisor_id": str(i), "name": "Person " + str(i), "email": "person" + str(i) + "@example.invalid",
             "phone": "404555" + str(i).zfill(4), "city": "Atlanta", "state": "GA" if i < 90 else "FL",
             "address1": str(i) + " Main Street", "title": "Advisor", "record_type": "Advisor" if i < 80 else "Staff",
             "profile_url": "https://example.invalid/person/" + str(i)} for i in range(count)]


def write(path, records, fields=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or list(records[0]), extrasaction="ignore")
        writer.writeheader(); writer.writerows(records)
    return path


class EdwardJonesNativeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'baseline.csv'
        self.candidate = Path(self.temp.name) / 'candidate.csv'
        self.rows = [dict(fid=str(i), faEntityId='shared-branch-entity',
            faName='Person ' + str(i), faCity='Atlanta',
            faState='GA' if i < 90 else 'FL', phone='404555' + str(i).zfill(4),
            address=str(i) + ' Main Street', email='person' + str(i) + '@example.invalid',
            faUrl='/us-en/financial-advisor/person-' + str(i)) for i in range(100)]
        write(self.base, self.rows)

    def compare(self, current):
        before = self.base.read_bytes()
        write(self.candidate, current)
        result = history.compare_roster(self.candidate, self.base, 'edward_jones')
        self.assertEqual(self.base.read_bytes(), before)
        return result

    def test_native_schema_uses_distinct_fid_not_shared_entity(self):
        result = self.compare(self.rows)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['candidate']['sourceEntities'], 100)
        self.assertEqual(result['candidate']['identityKinds'], {'provider': 100})
        self.assertEqual(result['candidate']['fieldsPresent']['name'], 100)
        self.assertEqual(result['candidate']['stateCounts'], {'ga': 90, 'fl': 10})
        self.assertEqual(result['continuity'], dict(retained=100, removed=0, added=0))
        self.assertEqual(result['policyVersion'], 'roster-history-v5:edward_jones')

    def test_missing_fid_never_falls_back_to_nonunique_entity_id(self):
        current = [dict(row, fid='') for row in self.rows]
        people, summary, owners = history._roster_summary(current, 'edward_jones')
        self.assertEqual(len(people), 100)
        self.assertEqual(summary['identityKinds'], {'profile_and_name': 100})
        self.assertFalse(any('faEntityId' in key for key in people))

    def test_native_metadata_losses_are_not_hidden(self):
        for field in ['faName', 'faCity', 'faState', 'faUrl']:
            with self.subTest(field=field):
                current = [dict(row) for row in self.rows]
                for row in current[:30]:
                    row[field] = ''
                result = self.compare(current)
                self.assertEqual(result['status'], 'held')
                self.assertIn('FIELD_COVERAGE_LOST', {r['code'] for r in result['issues']})

    def test_native_provider_replacements_are_held(self):
        current = [dict(row) for row in self.rows]
        for row in current[:25]:
            row['fid'] = 'replacement-' + row['fid']
        result = self.compare(current)
        self.assertIn('SOURCE_ENTITIES_DISAPPEARED', {r['code'] for r in result['issues']})

    def test_native_email_swap_is_held(self):
        current = [dict(row) for row in self.rows]
        current[0]['email'], current[1]['email'] = current[1]['email'], current[0]['email']
        result = self.compare(current)
        self.assertIn('EMAIL_PERSON_ASSOCIATION_CHANGED', {r['code'] for r in result['issues']})

    def test_native_aliases_do_not_change_other_providers(self):
        row = dict(name='Generic Name', faName='Native Name', faEntityId='not-person-id')
        self.assertEqual(history._project(row, 'ameriprise')['name'], 'generic name')
        self.assertEqual(history._project(row, 'edward_jones')['name'], 'native name')


class HistoricalRosterPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.stage = Path(self.temp.name)
        self.old = write(self.stage / "data/raw/firm_rosters/ameriprise_2026-09-22.csv", rows())
        self.candidate = write(self.stage / "incoming/ameriprise_2026-10-23.csv", rows())
        self.before = self.old.read_bytes()

    def compare(self, records=None, slug="ameriprise"):
        if records is not None: write(self.candidate, records)
        result = history.compare_roster(self.candidate, self.old, slug)
        self.assertEqual(self.old.read_bytes(), self.before)
        return result

    def codes(self, records):
        result = self.compare(records)
        self.assertEqual(result["status"], "held")
        return {item["code"] for item in result["issues"]}

    def test_same_data_and_order_changes_are_accepted(self):
        for records in (rows(), list(reversed(rows()))):
            result = self.compare(records)
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(result["continuity"]["retained"], 100)

    def test_provider_ids_catch_replaced_people_with_unchanged_count(self):
        records = rows()
        for row in records[:25]: row["advisor_id"] = "replacement-" + row["advisor_id"]
        self.assertIn("SOURCE_ENTITIES_DISAPPEARED", self.codes(records))

    def test_all_populated_channels_and_metadata_are_checked_separately(self):
        for field in ("email", "phone", "title", "address1", "city", "state", "profile_url"):
            records = rows()
            for row in records[:30]: row[field] = ""
            self.assertIn("FIELD_COVERAGE_LOST", self.codes(records))

    def test_localized_state_or_staff_loss_is_not_hidden_by_national_total(self):
        records = rows()
        for row in records[90:]: row["state"] = "GA"
        self.assertIn("STATE_COVERAGE_LOST", self.codes(records))
        records = rows()
        for row in records[80:]: row["record_type"] = "Advisor"
        self.assertIn("SOURCE_SEGMENT_COVERAGE_LOST", self.codes(records))

    def test_newly_populated_fields_are_improvements_not_replacements(self):
        original = rows()
        for row in original[:30]: row["email"] = ""
        write(self.old, original); self.before = self.old.read_bytes()
        result = self.compare(rows())
        self.assertEqual("accepted", result["status"])
        self.assertEqual({"gained": 30, "lost": 0, "replaced": 0}, result["fieldTransitions"]["email"])

    def test_population_mix_does_not_hide_or_invent_retained_field_loss(self):
        records = rows(120)
        for row in records[100:]: row["title"] = ""
        result = self.compare(records)
        self.assertEqual("accepted", result["status"])
        self.assertTrue(any("Population changed field density" in warning for warning in result["warnings"]))
        for row in records[:10]: row["title"] = ""
        self.assertIn("FIELD_COVERAGE_LOST", self.codes(records))

    def test_small_provider_rekey_with_same_named_email_owner_is_not_a_swap(self):
        original=rows();original[0]['name']='Alice Smith'
        write(self.old,original);self.before=self.old.read_bytes()
        current=[dict(row) for row in original];current[0]['advisor_id']='new-id'
        result=self.compare(current)
        self.assertEqual('accepted',result['status'])
        self.assertEqual(1,result['comparisonOnlyEmailOwnerNameContinuity'])
        self.assertEqual(1,result['continuity']['removed'])
        current[0]['name']='Bob Smith'
        self.assertIn('EMAIL_PERSON_ASSOCIATION_CHANGED',self.codes(current))

    def test_small_email_swap_is_held_even_below_global_change_limit(self):
        records = rows()
        records[0]["email"], records[1]["email"] = records[1]["email"], records[0]["email"]
        self.assertIn("EMAIL_PERSON_ASSOCIATION_CHANGED", self.codes(records))

    def test_known_duplicate_rows_are_reported_but_new_duplicates_are_held(self):
        records = rows(); records.append(dict(records[0]))
        self.assertIn("NEW_DUPLICATE_SOURCE_ROWS", self.codes(records))
        write(self.old, records); self.before = self.old.read_bytes()
        self.assertEqual(self.compare(records)["status"], "accepted")

    def test_multiple_offices_for_one_provider_person_are_not_duplicate_ids(self):
        records = rows()
        extra = dict(records[0]); extra["address1"] = "Second office"
        records.append(extra)
        write(self.old, records); self.before = self.old.read_bytes()
        result = self.compare(records)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["candidate"]["records"], 101)
        self.assertEqual(result["candidate"]["sourceEntities"], 100)

    def test_same_provider_id_with_new_conflicting_names_is_held(self):
        records = rows(); records[0]["advisor_id"] = records[1]["advisor_id"]
        self.assertIn("SOURCE_ID_AMBIGUITY", self.codes(records))

    def test_profile_or_name_location_fallback_is_explicit(self):
        result = self.compare(slug="corient")
        self.assertEqual(result["candidate"]["identityKinds"], {"profile_and_name": 100})
        records = rows()
        for row in records: row["profile_url"] = ""
        write(self.old, records); self.before = self.old.read_bytes()
        result = self.compare(records, slug="corient")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["candidate"]["identityKinds"], {"name_location": 100})
        self.assertTrue(result["warnings"])

    def test_no_baseline_is_truthfully_initial_not_a_historical_pass(self):
        result = history.compare_roster(self.candidate, None, "ameriprise")
        self.assertEqual(result["status"], "accepted")
        self.assertIsNone(result["baselineSha256"])
        self.assertIn("Initial snapshot", result["warnings"][0])

    def test_truncated_rows_and_duplicate_headers_cannot_replace_baseline(self):
        with self.candidate.open("a") as handle: handle.write("partial,row\n")
        self.assertEqual(self.compare()["issues"][0]["code"], "CSV_FORMAT_INVALID")
        write(self.candidate, rows(), fields=list(rows()[0]) + ["email"])
        self.assertEqual(self.compare()["issues"][0]["code"], "CSV_FORMAT_INVALID")

    def test_json_email_collections_preserve_same_row_associations(self):
        original = rows()
        for row in original: row["email"] = json.dumps([row["email"]])
        write(self.old, original); self.before = self.old.read_bytes()
        self.assertEqual(self.compare(original)["status"], "accepted")
        self.assertEqual(self.compare(original)["candidate"]["fieldsPresent"]["email"], 100)

    def test_aggregate_report_contains_no_names_or_email_addresses(self):
        result = self.compare()
        text = json.dumps(result)
        self.assertNotIn("Person 0", text)
        self.assertNotIn("person0@example.invalid", text)
        self.assertNotIn("0 Main Street", text)

    def test_exact_baseline_file_set_is_bound_to_generic_assessment(self):
        result = history.compare_source(self.stage, "roster:ameriprise", self.candidate)
        self.assertEqual(result["baselineFacts"], core.facts([self.old], self.stage))
        self.assertEqual(result["candidateSha256"], core.file_hash(self.candidate))


if __name__ == "__main__":
    unittest.main()
