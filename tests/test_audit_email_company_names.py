import csv
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from audit_email_company_names import audit, audit_roster_labels, proposal, run, safe_cell
from identity_schema import runtime_content_hash


class CompanyNameAuditTests(unittest.TestCase):
    def test_aliases_preserve_acronyms(self):
        self.assertEqual(proposal('UBS Financial Services Inc.')[0], 'UBS')
        self.assertEqual(proposal('LPL Financial LLC')[0], 'LPL Financial')
        self.assertEqual(proposal('RBC Wealth Management')[0], 'RBC Wealth Management')
        self.assertEqual(proposal('Raymond James (branch pages)')[0], 'Raymond James')

    def test_distinct_brands_are_not_parent_collapsed(self):
        for name in ('Alex. Brown (Raymond James)', 'Graystone Consulting from Morgan Stanley',
                     'Wells Fargo Advisors Financial Network'):
            p = proposal(name)
            self.assertEqual(p[0], name)
            self.assertTrue(p[3])

    def test_unknown_and_legacy_not_guessed(self):
        self.assertEqual(proposal('Unfamiliar Boutique')[0], 'Unfamiliar Boutique')
        self.assertEqual(proposal('SunTrust')[0], 'SunTrust')
        self.assertTrue(proposal('SunTrust')[3])
        self.assertEqual(proposal('Rock')[0], 'Rock')

    def test_spelling_and_suffix_are_only_review_proposals(self):
        self.assertEqual(proposal('Wlliam Blair')[:2], ('William Blair', 'review_spelling'))
        self.assertEqual(proposal('Boutique Advisors, LLC')[:2], ('Boutique Advisors', 'review_legal_suffix'))

    def test_csv_formula_cells(self):
        for value in ('=1+1', '+CMD', '-1', '@formula', '  =1'):
            self.assertTrue(safe_cell(value).startswith("'"))
        self.assertEqual(safe_cell('& Partners'), '& Partners')

    def test_roster_scope_and_distinct_brand(self):
        payload = {'recipients': {
            '1': dict(firm='UBS Financial Services Inc.', eligible=True, source='CRM'),
            '2': dict(firm='Equity Investment Corporation', eligible=True, source='EIC', internal=True),
            '3': dict(firm='UBS', eligible=True, source='UBS'),
            '4': dict(firm='Alex. Brown (Raymond James)', eligible=True, source='Alex. Brown (Raymond James)'),
            '5': dict(firm='Unknown', eligible=True, source=''),
        }}
        rows = audit_roster_labels(payload)
        self.assertEqual(sum(r['recipient_count'] for r in rows), 2)
        self.assertEqual({r['current_company_name'] for r in rows}, {'UBS', 'Alex. Brown (Raymond James)'})
        alex = next(r for r in rows if r['current_company_name'].startswith('Alex.'))
        self.assertEqual(alex['proposed_email_display_name'], 'Alex. Brown')
        self.assertTrue(alex['needs_review'])
        self.assertFalse(alex['application_change_applied'])

    def test_counts_and_no_mutation(self):
        payload = {'recipients': {'1': dict(firm='UBS', eligible=True, source='CRM', email='a@ubs.com'),
                                  '2': dict(firm='UBS Financial Services Inc.', eligible=True, source='UBS', email='b@ubs.com')}}
        before = json.dumps(payload, sort_keys=True)
        rows = audit(payload)
        self.assertEqual(sum(r['recipient_count'] for r in rows), 2)
        self.assertEqual(json.dumps(payload, sort_keys=True), before)
        self.assertEqual(sum(r['proposed_change'] for r in rows), 1)

    def test_export_integrity_new_only_and_no_pii(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'data/identity/approved_recipients.json.gz'
            descriptor = root/'api/shared/approved-recipient-release.json'
            source.parent.mkdir(parents=True); descriptor.parent.mkdir(parents=True)
            core = dict(schemaVersion=1, recipients={'1': dict(firm='UBS Financial Services Inc.', eligible=True,
                email='private-person@ubs.com', source='CRM', name='Private Person')}, ineligible={}, provenance={})
            payload = dict(core, generated='test', contentHash=runtime_content_hash(core))
            source.write_bytes(gzip.compress(json.dumps(payload).encode()))
            descriptor.write_text(json.dumps(dict(registryContentHash=payload['contentHash'], recipientCount=1)))
            original = source.read_bytes()
            output = root/'data/output/audit'
            result = run(root, output)
            self.assertTrue(result['sources_unchanged'])
            self.assertEqual(source.read_bytes(), original)
            with (output/'company_name_audit.csv').open(encoding='utf-8-sig', newline='') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]['proposed_email_display_name'], 'UBS')
            text = (output/'company_name_audit.csv').read_text(encoding='utf-8-sig')
            self.assertNotIn('private-person@', text)
            self.assertNotIn('Private Person', text)
            with self.assertRaises(ValueError): run(root, output)
            payload['recipients']['1']['firm'] = 'Changed'
            source.write_bytes(gzip.compress(json.dumps(payload).encode()))
            with self.assertRaises(ValueError): run(root, root/'data/output/other')


if __name__ == '__main__':
    unittest.main()
