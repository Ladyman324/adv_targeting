import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from roster_entities import cetera_person, quarantine_shared_emails,quarantine_morgan_ui_claims,morgan_ui_claim_correlates


class RosterEntityTests(unittest.TestCase):
    def test_morgan_ui_identifiers_do_not_assign_other_peoples_mailboxes(self):
        rows=[{'Name':name,'Email':address,'Email Source':'ui_identifier','Main Phone':'7325784560'}
              for name,address in [('Darlene Piraino','julia.ivanitski@morganstanley.com'),
                                   ('Janet Gaglione','mary.ann.jacobs@morganstanley.com'),
                                   ('Mary Ann Jacobs','mary.ann.jacobs@morganstanley.com')]]
        output,review=quarantine_morgan_ui_claims(rows)
        self.assertEqual(['','','mary.ann.jacobs@morganstanley.com'],[r['Email'] for r in output])
        self.assertEqual(2,len(review));self.assertEqual('7325784560',output[0]['Main Phone'])
        self.assertEqual('julia.ivanitski@morganstanley.com',rows[0]['Email'])
        self.assertEqual(['julia.ivanitski@morganstanley.com'],json.loads(output[0]['Published Email Claims']))

    def test_name_correlation_is_not_a_crd_or_nickname_inference(self):
        for name,address in [('Michael Balsom','brad.balsom@morganstanley.com'),('George Smith','gs@morganstanley.com'),('Renee Morgan','renee.r.morgan@morganstanley.com')]:
            self.assertTrue(morgan_ui_claim_correlates(name,address))
        self.assertFalse(morgan_ui_claim_correlates('Maria Llamas','service-team@morganstanley.com'))

    def test_short_names_require_whole_mailbox_components(self):
        for name,address in [('Vu Ho','vu.ho@morganstanley.com'),
                             ('Nichole Yu','nicole.yu@morganstanley.com'),
                             ('Jake Hu','jinfan.hu@morganstanley.com')]:
            self.assertTrue(morgan_ui_claim_correlates(name,address))
        self.assertFalse(morgan_ui_claim_correlates('Al Ng','sales-team@morganstanley.com'))
        self.assertFalse(morgan_ui_claim_correlates('Vu Ho','service-hotline@morganstanley.com'))
    def test_office_with_associated_crd_is_not_a_person(self):
        self.assertFalse(cetera_person({'name': 'Tampa', 'advisor_crd': '123456', 'first_name': '', 'last_name': ''}))
        self.assertTrue(cetera_person({'first_name': 'Jason', 'last_name': 'Hughley', 'advisor_crd': '123456'}))
        for missing in (None, float('nan'), '', 'null'):
            self.assertFalse(cetera_person({'first_name': missing, 'last_name': 'Office'}))

    def test_conflicting_email_claims_are_preserved_for_review_not_authority(self):
        inputs = [{'MarketingName': 'Alan Voorhees', 'Emails': 'gil.blitz@ubs.com', 'Phone': '111'},
                  {'MarketingName': 'Gilbert Blitz', 'Emails': '["gil.blitz@ubs.com", "gilbert.blitz@ubs.com"]'}]
        output, review = quarantine_shared_emails(inputs)
        self.assertEqual('', output[0]['Emails'])
        self.assertEqual('111', output[0]['Phone'])
        self.assertEqual('gilbert.blitz@ubs.com', output[1]['Emails'])
        self.assertEqual(['gil.blitz@ubs.com'], json.loads(output[0]['disputed_email_claims']))
        self.assertEqual(2, len(review))
        self.assertEqual('gil.blitz@ubs.com', inputs[0]['Emails'])

    def test_credentials_and_known_nickname_do_not_invent_shared_email_conflict(self):
        records = [{'MarketingName': name, 'Emails': 'chris@example.invalid'}
                   for name in ('Christopher Tolman, CFP', 'Chris Tolman')]
        output, review = quarantine_shared_emails(records)
        self.assertEqual([], review)
        self.assertTrue(all(row['Emails'] == 'chris@example.invalid' for row in output))

    def test_different_generations_remain_a_conflict(self):
        records = [{'MarketingName': name, 'Emails': 'shared@example.invalid'}
                   for name in ('Lynn Shaw Jr', 'Lynn Shaw Sr')]
        output, review = quarantine_shared_emails(records)
        self.assertEqual(2, len(review))
        self.assertTrue(all(not row['Emails'] for row in output))
