from __future__ import annotations

import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from refresh_reports import compare, semantic_content_hash


def put(root, relative, obj):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding='utf-8')


def parquet(root, relative, rows):
    import pandas as pd
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)


class ReportTests(unittest.TestCase):
    def test_only_defined_operational_timestamps_ignored(self):
        self.assertEqual(semantic_content_hash({'as_of': 'old', 'x': 1}),
                         semantic_content_hash({'as_of': 'new', 'x': 1}))
        self.assertNotEqual(semantic_content_hash({'employmentDate': 'old'}),
                            semantic_content_hash({'employmentDate': 'new'}))

    def test_contact_changes_territory_and_eligibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            before, after, out = [Path(tmp) / name for name in ('before', 'after', 'reports')]
            for root, email, state in ((before, 'old@x.com', 'GA'), (after, 'new@x.com', 'FL')):
                put(root, 'webapp/data/contacts.json', {'advisors': {'1': {'e': email}}})
                put(root, 'webapp/data/advisor_index.json', {
                    'firms': ['Firm'], 'cities': ['City'],
                    'advisors': [['1', 'Name One', 0, state, 0, state]]})
                put(root, 'webapp/data/territories.json', {
                    'states': {'GA': {'c': 'MK'}, 'FL': {'c': 'SB'}}})
            put(after, 'data/identity/approved_recipients.json', {
                'recipients': {'1': {'email': 'new@x.com', 'tier': 'high'}}})
            result = compare(before, after, out)
            self.assertEqual(result['categories']['contacts']['changes'], 1)
            self.assertEqual(result['categories']['map']['changes'], 2)
            self.assertEqual(result['categories']['recipient_eligibility']['added'], 1)
            self.assertTrue((out / 'comparison.json').is_file())

    def test_asset_sum_uses_accounts_not_shared_advisor_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            before, after, out = [Path(tmp) / name for name in ('before', 'after', 'reports')]
            put(after, 'webapp/data/act_assets.json', {
                'columns': ['acv'], 'accounts': [[100]], 'totals': {'acv': 100},
                'advisors': {'1': {'acv': 100}, '2': {'acv': 100}}})
            result = compare(before, after, out)
            self.assertEqual(result['assets']['after']['accountVectorTotals']['acv'], 100)
            self.assertEqual(result['assets']['after']['issues'], [])

    def test_inconsistent_assets_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            before, after, out = [Path(tmp) / name for name in ('before', 'after', 'reports')]
            put(after, 'webapp/data/act_assets.json', {
                'columns': ['acv'], 'accounts': [[100]], 'totals': {'acv': 50}})
            self.assertTrue(compare(before, after, out)['assets']['after']['issues'])

    def test_private_csv_formula_safe_and_removals_not_departures(self):
        with tempfile.TemporaryDirectory() as tmp:
            before, after, out = [Path(tmp) / name for name in ('before', 'after', 'reports')]
            put(before, 'webapp/data/contacts.json', {'advisors': {'=evil()': {'n': '=bad()'}}})
            result = compare(before, after, out)
            with (out / 'contacts.csv').open(encoding='utf-8-sig') as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row['id'], "'=evil()")
            self.assertEqual(row['change'], 'removed_from_artifact')
            self.assertIn('not a verified departure', result['warning'])

    def test_forbids_public_report_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, 'public'):
                compare(root, root, root / 'webapp/data/reports')

    def test_identical_inputs_produce_no_business_diffs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = compare(root / 'before', root / 'after', root / 'reports')
            self.assertTrue(all(row['changes'] == 0 for row in result['categories'].values()))
            self.assertFalse(result['artifactAvailability']['after']['webapp/data/contacts.json'])

    def test_same_contact_count_channel_loss_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            before, after, out = [Path(tmp) / name for name in ('before', 'after', 'reports')]
            put(before, 'webapp/data/contacts.json', {'advisors': {
                '1': {'e': 'one@x.com', 'w': '+14045550001'}, '2': {'e': 'two@x.com'}}})
            put(after, 'webapp/data/contacts.json', {'advisors': {
                '1': {'e': '', 'w': ''}, '2': {'e': 'two@x.com'}}})
            result = compare(before, after, out)
            self.assertEqual(result['coverage']['before']['contacts'], result['coverage']['after']['contacts'])
            self.assertEqual(result['coverage']['before']['emailAddresses'], 2)
            self.assertEqual(result['coverage']['after']['emailAddresses'], 1)
            self.assertEqual(result['coverage']['after']['phoneNumbers'], 0)
            self.assertEqual(result['coverage']['after']['contactable'], 1)

    def test_street_coordinate_and_placement_changes_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            before, after, out = [Path(tmp) / name for name in ('before', 'after', 'reports')]
            for root, street, latitude in ((before, '100 Old Street', 33.1), (after, '200 New Street', 33.2)):
                parquet(root, 'data/interim/contact_work_branches.parquet', [{
                    'advisor_crd': '1', 'firm_crd': '10', 'branch_street1': street,
                    'branch_city': 'Atlanta', 'branch_state': 'GA', 'lat': latitude, 'lon': -84.1}])
                parquet(root, 'data/interim/advisor_placement.parquet', [{
                    'advisor_crd': '1', 'firm_crd': '10', 'addr_key': street,
                    'location_type': 'office', 'uncertain': False}])
            result = compare(before, after, out)
            self.assertEqual(result['categories']['working_offices']['changes'], 1)
            self.assertEqual(result['categories']['placements']['changes'], 1)
            self.assertIn('200 New Street', (out / 'working_offices.csv').read_text(encoding='utf-8-sig'))
            self.assertNotIn('200 New Street', json.dumps(result))

    def test_employment_history_changes_not_collapsed_and_row_order_irrelevant(self):
        with tempfile.TemporaryDirectory() as tmp:
            before, after, out = [Path(tmp) / name for name in ('before', 'after', 'reports')]
            employments = [{'advisor_crd': '1', 'firm_crd': '10', 'emp_state': 'GA'},
                           {'advisor_crd': '1', 'firm_crd': '20', 'emp_state': 'FL'}]
            for root, records in ((before, employments), (after, list(reversed(employments)))):
                parquet(root, 'data/interim/advisor_employments.parquet', records)
            for root, end in ((before, None), (after, '10/2026')):
                parquet(root, 'data/interim/advisor_employment_history.parquet', [{
                    'advisor_crd': '1', 'firm_name_on_record': 'Firm',
                    'from_date': '01/2020', 'to_date': end}])
            result = compare(before, after, out)
            self.assertEqual(result['categories']['sec_employments']['changes'], 0)
            self.assertEqual(result['categories']['sec_employment_history']['changes'], 1)

    def test_ranking_review_unmatched_ambiguous_and_search_manifest_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            before, after, out = [Path(tmp) / name for name in ('before', 'after', 'reports')]
            for root, crd, tier, ambiguous in ((before, '1', 'high', False), (after, '', 'review', True)):
                parquet(root, 'data/interim/forbes_matches.parquet', [{
                    'advisor_name': 'Name One', 'forbes_uri': 'name-one', 'year': 2026,
                    'category': 'Georgia', 'advisor_crd': crd, 'match_tier': tier, 'ambiguous': ambiguous}])
            parquet(after, 'data/interim/barrons_rankings.parquet', [{
                'advisor_name': 'Name Two', 'barrons_url': 'example', 'ranking': 'top100',
                'year': 2026, 'advisor_crd': None}])
            put(after, 'webapp/data/advisor_search.json', {'advisors': 100})
            put(after, 'webapp/data/name_index.json', {'searchable': 60, 'unplaced': 5})
            result = compare(before, after, out)
            self.assertEqual(result['categories']['forbes_matches']['changes'], 1)
            self.assertEqual(result['rankingMatchCoverage']['after']['forbes']['ambiguous'], 1)
            self.assertEqual(result['rankingMatchCoverage']['after']['forbes']['unmatched'], 1)
            self.assertEqual(result['rankingMatchCoverage']['after']['barrons']['unmatched'], 1)
            self.assertEqual(result['coverage']['after']['desktopSearch'], 100)
            self.assertEqual(result['coverage']['after']['fieldSearch'], 60)
            self.assertEqual(result['coverage']['after']['fieldUnplaced'], 5)
            self.assertIsNone(result['coverage']['before']['desktopSearch'])
            self.assertTrue(result['artifactAvailability']['after']['webapp/data/name_index.json'])


if __name__ == '__main__':
    unittest.main()
