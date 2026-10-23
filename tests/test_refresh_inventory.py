from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from refresh_inventory import candidate_inventory, inventory, snapshot_facts, validate_act_snapshot


def put(root, name, obj):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding='utf-8')
    return path


def act_pair(root, stamp='2026-09-25T022553Z'):
    contacts = put(root, f'data/raw/act_contacts_{stamp}.json', [{'id': 'a1'}])
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    owners = put(root, f'data/raw/act_eic_contact_{stamp}.json', {
        'schema_version': 1, 'pull_id': stamp, 'contacts_file': contacts.name,
        'contacts_sha256': sha(contacts), 'contacts_rows': 1, 'complete': True,
        'failed_codes': [], 'expected_codes': ['AA'], 'queried_codes': ['AA'],
        'counts': {'AA': 1}, 'conflicts': [], 'owner_by_contact_id': {'a1': 'AA'}})
    put(root, f'data/raw/act_pull_manifest_{stamp}.json', {
        'schema_version': 1, 'policies': {'pull_id': stamp, 'pair_complete': True,
                                        'partial_refreshes': 'forbidden'},
        'inputs': [{'role': role, 'file': p.name, 'sha256': sha(p),
                    'bytes': p.stat().st_size, 'rows': 1, 'complete': True}
                   for role, p in [('act_contacts', contacts), ('act_eic_contact', owners)]]})
    return contacts


class InventoryTests(unittest.TestCase):
    def test_valid_pair_and_no_mtime_retrieval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            act_pair(root)
            roster = root / 'data/raw/firm_rosters/ubs_2026-09-01.csv'
            roster.parent.mkdir(parents=True)
            roster.write_text('Name,Email\nOne,one@ubs.com\n', encoding='utf-8')
            entries = {row['id']: row for row in inventory(root)}
            self.assertEqual(entries['act']['records'], 1)
            self.assertEqual(entries['act']['retrievedUtc'], '2026-09-25T02:25:53Z')
            self.assertIsNone(entries['roster:ubs']['retrievedUtc'])
            self.assertEqual(entries['roster:ubs']['records'], 1)
            self.assertEqual(entries['roster:ubs']['snapshotDate'], '2026-09-01')

    def test_newer_orphan_fails_instead_of_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            act_pair(root)
            put(root, 'data/raw/act_contacts_2026-10-01T000000Z.json', [])
            with self.assertRaisesRegex(SystemExit, 'orphan'):
                validate_act_snapshot(root)
            entry = next(row for row in inventory(root) if row['id'] == 'act')
            self.assertEqual(entry['status'], 'failed')
            self.assertIn('orphan', entry['error'])

    def test_modified_pair_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contact = act_pair(root)
            contact.write_text('[]', encoding='utf-8')
            with self.assertRaises(SystemExit):
                validate_act_snapshot(root)

    def test_sec_edition_selection_chronological_not_month_lexical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('IA_INDVL_Feed_12_01_2025.xml.zip',
                         'IA_INDVL_Feed_01_01_2026.xml.zip'):
                put(root, 'data/raw/' + name, {})
            entry = next(row for row in inventory(root) if row['id'] == 'sec_individuals')
            self.assertEqual(entry['sourceDate'], '2026-01-01')
            self.assertIsNone(entry['retrievedUtc'])

    def test_facts_content_changes_but_not_touch_changes_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = put(root, 'a.json', {'one': 1})
            first = snapshot_facts(root, [path])
            path.touch()
            self.assertEqual(first, snapshot_facts(root, [path]))
            path.write_text('{}', encoding='utf-8')
            self.assertNotEqual(first['snapshotId'], snapshot_facts(root, [path])['snapshotId'])

    def test_rankings_keep_edition_without_guessing_retrieval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            put(root, 'data/raw/barrons_rankings.json', [{'year': 2026}, {'year': 2025}])
            entry = next(row for row in inventory(root) if row['id'] == 'barrons')
            self.assertEqual(entry['edition'], '2025, 2026')
            self.assertIsNone(entry['retrievedUtc'])

    def test_immutable_rankings_history_changes_bound_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            put(root, 'data/raw/barrons_rankings.json', [{'year': 2026}])
            relative = 'data/refresh/ranking_editions/barrons/top100-2026.json'
            put(root, relative, [{'year': 2026, 'rank': 1}])
            first = next(row for row in inventory(root) if row['id'] == 'barrons')
            put(root, relative, [{'year': 2026, 'rank': 2}])
            second = next(row for row in inventory(root) if row['id'] == 'barrons')
            self.assertTrue(first['immutableHistory'])
            self.assertIn(relative, first['files'])
            self.assertNotEqual(first['snapshotId'], second['snapshotId'])

    def test_failed_receipt_keeps_last_good_retrieval_and_real_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            act_pair(root)
            baseline = next(row for row in inventory(root) if row['id'] == 'act')
            put(root, 'data/refresh/source_receipts.json', {'act': {
                'lastAttemptUtc': '2026-10-01T00:00:00Z', 'attemptStatus': 'failed',
                'status': 'failed', 'errorCode': 'ACQUISITION_FAILED',
                'records': 999, 'sha256': 'not-evidence', 'files': ['elsewhere']}})
            result = next(row for row in inventory(root) if row['id'] == 'act')
            self.assertEqual(result['retrievedUtc'], baseline['retrievedUtc'])
            self.assertEqual(result['lastSuccessUtc'], baseline['lastSuccessUtc'])
            self.assertEqual(result['lastAttemptUtc'], '2026-10-01T00:00:00Z')
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['records'], 1)
            self.assertEqual(result['sha256'], baseline['sha256'])
            self.assertEqual(result['files'], baseline['files'])

    def test_receipt_cannot_hide_observed_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = act_pair(root)
            path.write_text('[]', encoding='utf-8')
            put(root, 'data/refresh/source_receipts.json', {'act': {
                'attemptStatus': 'succeeded', 'status': 'available'}})
            result = next(row for row in inventory(root) if row['id'] == 'act')
            self.assertEqual(result['status'], 'failed')

    def test_supporting_footprint_preserves_decisions_and_geocode_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            put(root, 'data/identity/act_identity_decisions.json', {})
            put(root, 'data/interim/contact_work_geocodes.csv', {})
            rows = {row['id']: row for row in inventory(root)}
            self.assertTrue(rows['identity_decisions']['sha256'])
            self.assertTrue(rows['contact_geocode_cache']['sha256'])

    def test_explicit_imported_adv_is_bound_instead_of_old_legacy_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            put(root, 'data/raw/ia050126.zip', {})
            put(root, 'data/raw/refresh_adv_abcdef.csv', {})
            observed = next(row for row in inventory(root) if row['id'] == 'sec_adv')
            put(root, 'data/refresh/source_receipts.json', {'sec_adv': {
                'snapshotId': observed['snapshotId'],
                'sourceDate': '2026-10-01', 'status': 'imported', 'attemptStatus': 'success'}})
            row = next(row for row in inventory(root) if row['id'] == 'sec_adv')
            self.assertEqual(row['files'], ['data/raw/refresh_adv_abcdef.csv'])
            self.assertEqual(row['sourceDate'], '2026-10-01')
            self.assertIsNone(row['retrievedUtc'])

    def test_stale_and_legacy_receipts_cannot_relabel_selected_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            act_pair(root)
            baseline = next(row for row in inventory(root) if row['id'] == 'act')
            for binding in ({}, {'snapshotId': 'different-snapshot'},
                            {'snapshotId': baseline['snapshotId']}):
                put(root, 'data/refresh/source_receipts.json', {'act': {
                    **binding, 'retrievedUtc': '2020-01-01T00:00:00Z',
                    'lastSuccessUtc': '2020-01-01T00:00:00Z'}})
                current = next(row for row in inventory(root) if row['id'] == 'act')
                self.assertEqual(current['retrievedUtc'], baseline['retrievedUtc'])
                self.assertEqual(current['lastSuccessUtc'], baseline['lastSuccessUtc'])
                self.assertEqual(current['fileFacts'], baseline['fileFacts'])

    def test_bound_receipt_applies_only_to_exact_file_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = put(root, 'data/raw/IA_FIRM_SEC_Feed_10_01_2026.xml.gz', {})
            original = next(row for row in inventory(root) if row['id'] == 'sec_firms')
            put(root, 'data/refresh/source_receipts.json', {'sec_firms': {
                'snapshotId': original['snapshotId'],
                'retrievedUtc': '2026-10-02T00:00:00Z',
                'lastSuccessUtc': '2026-10-02T00:00:00Z',
                'sourceDate': '2020-01-01', 'status': 'acquired'}})
            reused = next(row for row in inventory(root) if row['id'] == 'sec_firms')
            self.assertEqual(reused['retrievedUtc'], '2026-10-02T00:00:00Z')
            self.assertEqual(reused['sourceDate'], '2026-10-01')
            path.write_text('{"changed": true}', encoding='utf-8')
            changed = next(row for row in inventory(root) if row['id'] == 'sec_firms')
            self.assertIsNone(changed['retrievedUtc'])
            self.assertIsNone(changed['lastSuccessUtc'])
            self.assertNotEqual(changed['snapshotId'], original['snapshotId'])

    def test_candidate_cached_sec_uses_interim_and_filtered_outputs_unknown_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            put(root, 'data/raw/IA_INDVL_Feed_07_20_2026.xml.zip', {})
            interim = put(root, 'data/interim/advisors.parquet', {'unfiltered': True})
            exported = put(root, 'data/output/advisors.parquet', {'filtered': True})
            put(root, 'data/interim/firm_websites.parquet', {'url': 'example'})
            original = inventory(root)
            original_json = json.dumps(original, sort_keys=True)
            rows = {row['id']: row for row in candidate_inventory(root, 'act-only', original)}
            entry = rows['sec_individuals']
            self.assertEqual(set(entry['files']), {
                'data/interim/advisors.parquet', 'data/output/advisors.parquet'})
            facts = {fact['file']: fact for fact in entry['fileFacts']}
            self.assertEqual(facts['data/interim/advisors.parquet']['sha256'],
                             hashlib.sha256(interim.read_bytes()).hexdigest())
            self.assertEqual(facts['data/output/advisors.parquet']['sha256'],
                             hashlib.sha256(exported.read_bytes()).hexdigest())
            self.assertEqual(entry['status'], 'reused')
            self.assertIsNone(entry['sourceDate'])
            self.assertIsNone(entry['retrievedUtc'])
            self.assertEqual(entry['availableRawSnapshot']['sourceDate'], '2026-07-20')
            self.assertIn('data/interim/firm_websites.parquet', rows['sec_firms']['files'])
            entry['fileFacts'][0]['sha256'] = 'changed-in-result'
            self.assertEqual(json.dumps(original, sort_keys=True), original_json)

    def test_candidate_prefers_frozen_input_suppression_floor_over_generated_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            put(root, 'api/shared/act_mail_codes.json', {'generated': 'new'})
            floor = put(root, 'data/refresh/supporting_inputs/act_mail_codes.json', {'historical': 'keep'})
            original = inventory(root)
            row = next(row for row in candidate_inventory(root, 'act-only', original)
                       if row['id'] == 'act_preference_floor')
            self.assertEqual(row['files'], ['data/refresh/supporting_inputs/act_mail_codes.json'])
            self.assertEqual(row['fileFacts'][0]['sha256'], hashlib.sha256(floor.read_bytes()).hexdigest())
            old = next(row for row in original if row['id'] == 'act_preference_floor')
            self.assertEqual(old['files'], ['api/shared/act_mail_codes.json'])

    def test_policy_applies_without_source_receipts_and_specific_beats_wildcard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            put(root, 'refresh_policy.json', {'schemaVersion': 1,
                'freshnessDays': {'act': 3, 'roster:*': 60, 'roster:ubs': 10}})
            rows = {row['id']: row for row in inventory(root)}
            self.assertEqual(rows['act']['freshnessDays'], 3)
            self.assertEqual(rows['roster:ubs']['freshnessDays'], 10)
            self.assertEqual(rows['roster:morgan_stanley']['freshnessDays'], 60)
            self.assertFalse((root / 'data/refresh/source_receipts.json').exists())

    def test_full_candidate_keeps_raw_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            put(root, 'data/raw/IA_INDVL_Feed_07_20_2026.xml.zip', {})
            rows = {row['id']: row for row in candidate_inventory(root, 'full')}
            self.assertEqual(rows['sec_individuals']['files'],
                             ['data/raw/IA_INDVL_Feed_07_20_2026.xml.zip'])
            self.assertEqual(rows['sec_individuals']['sourceDate'], '2026-07-20')


if __name__ == '__main__':
    unittest.main()
