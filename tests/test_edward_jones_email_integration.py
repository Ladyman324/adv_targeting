"""Acquire/write/load must agree without changing other providers' policies."""
import copy
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import build_contacts as contacts
import edward_jones_async as source
from edward_jones_email import select_email


class EdwardJonesSelectionIntegrationTests(unittest.TestCase):
    def records(self):
        examples = [('Walter Smith', 'walter-smith', ["meg'an.smith", 'walter.smith2']),
                    ('Cheryl McCormick-Hann', 'cheryl-mccormick-hann',
                     ['other.person', 'cheryl.mccormick-hann']),
                    ('Price Wilkes', 'richard-wilkes', ['richard.wilkes']),
                    ('Donald Paul', 'donald-paul', ['donald.paul', 'donald.paul2'])]
        return [dict(fid=str(i), faEntityId='shared-not-a-person-id', faName=name,
            faUrl='/us-en/financial-advisor/' + slug, faCity='Atlanta', faState='GA',
            phone='4045550000', address=str(i) + ' Main St',
            email='stale.branch.staff@edwardjones.com',
            emails=json.dumps([local + '@edwardjones.com' for local in locals]))
                for i, (name, slug, locals) in enumerate(examples)]

    def test_acquisition_and_provider_resolver_agree(self):
        for row in self.records():
            candidates = json.loads(row['emails'])
            expected = select_email(row['faName'], candidates, row['faUrl']).email
            self.assertEqual(source.own_email(row['faName'], candidates, row['faUrl']) or '', expected)
            self.assertEqual(contacts.roster_own_email('edward_jones', row['faName'],
                candidates, row['faUrl']), expected)

    def test_real_roster_loader_uses_same_record_profile_not_stale_email(self):
        rows = self.records()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'edward_jones_2026-10-26.csv'
            with path.open('w', encoding='utf8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            with patch.object(contacts, 'ROSTERS', Path(directory)), patch.object(
                    contacts, 'FIRMS', {'edward_jones': {'label': 'Edward Jones', 'crds': ['250']}}):
                loaded = contacts.load_rosters()
            by_name = dict(zip(loaded['name'], loaded['email']))
        for row in rows:
            self.assertEqual(by_name[row['faName']], select_email(row['faName'],
                json.loads(row['emails']), row['faUrl']).email)

    def test_written_country_rows_preserve_branch_candidates(self):
        records = self.records()
        original = copy.deepcopy(records)
        facts = {'https://www.edwardjones.com' + row['faUrl']:
                 dict(emails=json.loads(row['emails']), status='ok', bio='Native bio', http_status=200)
                 for row in records}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'candidate.csv'
            source.write_enriched_records(records, facts, output, None)
            with output.open(encoding='utf-8-sig', newline='') as stream:
                written = list(csv.DictReader(stream))
        self.assertEqual(records, original)
        for before, after in zip(records, written):
            candidates = json.loads(before['emails'])
            self.assertEqual(json.loads(after['emails']), candidates)
            self.assertEqual(after['email'], select_email(before['faName'], candidates,
                before['faUrl']).email)
            self.assertEqual(after['bio'], 'Native bio')

    def test_other_firms_never_use_edward_jones_rules(self):
        with patch.object(contacts, 'own_email', return_value='unchanged@example.invalid') as legacy:
            with patch.object(contacts, 'select_edward_jones_email', side_effect=AssertionError('wrong provider')):
                for slug in ['ubs', 'wells_fargo', 'morgan_stanley', 'merrill']:
                    self.assertEqual(contacts.roster_own_email(slug, 'Name', ['mail']),
                                     'unchanged@example.invalid')
                self.assertEqual(legacy.call_count, 4)
