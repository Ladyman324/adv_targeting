"""Synthetic regressions for corroborated ACT names, Field book and staging I/O."""
from pathlib import Path
import sys
import tempfile
import unittest
import contextlib
import io
import json
import pandas as pd
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from identity_resolver import prepare_act_records, evaluate_assertion
from identity_normalize import given_agreement
from build_field_tiles import canonical_asset_amounts
import build_field_tiles as field_tiles
import refresh_acquire as acquire
import refresh_core as core


class CorroboratedNames(unittest.TestCase):
    def case(self, first='Katie', legal='Kaitlyn', middle='', act_middle=''):
        raw = {'id':'fixture', 'fullName':' '.join(filter(None, [first,act_middle,'Example'])),
            'firstName':first,'middleName':act_middle,'lastName':'Example', 'salutation':first,
            'emailAddress':legal.lower()+'.example@example.com', 'company':'Example Firm',
            'customFields':{'crd':'123'}, 'businessAddress':{
                'line1':'1 Main Street','city':'Example City','state':'NY','postalCode':'10001'}}
        record = prepare_act_records([raw], 'synthetic.json', 'synthetic')[0]
        sec = {'advisor_crd':'123','first_name':legal,'middle_name':middle,'last_name':'Example','suffix':''}
        context = {'current_firms':['77'], 'current_firm_names':['Example Firm'],
            'branches':[{'street':'1 Main Street','city':'Example City','state':'NY','postal':'10001'}]}
        return record, sec, context

    def test_katie_requires_email_and_paired_firm_location(self):
        record, sec, context = self.case()
        ev, link = evaluate_assertion(record, sec, context)
        self.assertEqual(link['identity_status'], 'approved')
        self.assertTrue(ev['given_corroborated'])
        self.assertFalse(ev['given_exact'])
        self.assertEqual(link['email_greeting'], 'Katie')
        self.assertIn('given_nickname_email_corroborated', ev['positive_evidence_json'])
        self.assertFalse(given_agreement(['Katie'], ['Kaitlyn'])[0])
        for changes in ({'norm_email':'other.example@example.com'}, {'raw_claimed_crd':''},
                        {'raw_postal':''}, {'crd_claim_count':2}, {'email_claim_count':2},
                        {'act_id_claim_count':2}, {'record_active':False},
                        {'raw_suffix':'Sr'}):
            with self.subTest(changes=changes):
                person = {**sec, 'suffix':'Jr'} if 'raw_suffix' in changes else sec
                self.assertNotEqual(evaluate_assertion({**record,**changes},person,context)[1]['identity_status'],'approved')

    def test_firm_domain_conflicts_and_split_branches_cannot_bridge(self):
        record, sec, context = self.case()
        variants = [
            {**context,'current_firm_names':['Different Firm']},
            {**context,'email_domain_policy':{'status':'authoritative','allowedFirmCrds':['88']}},
            {**context,'branches':[
                {'street':'1 Main Street','city':'Elsewhere','state':'NY','postal':'10001'},
                {'street':'2 Different Street','city':'Example City','state':'NY','postal':'10001'}]},
            {**context,'branches':[{'street':'1 Main Street','city':'Example City','state':'NY','postal':'10002'}]},
        ]
        for context in variants:
            with self.subTest(context=context):
                self.assertNotEqual(evaluate_assertion(record,sec,context)[1]['identity_status'],'approved')

    def test_full_middle_name_plus_matching_initial_not_initial_alone(self):
        record, sec, context = self.case('W.', 'William', 'Martin', 'Martin')
        record['raw_salutation'] = 'Martin'
        ev, link = evaluate_assertion(record,sec,context)
        self.assertEqual(link['identity_status'],'approved')
        self.assertEqual(link['email_greeting'],'Martin')
        self.assertIn('given_initial_full_middle_corroborated', ev['positive_evidence_json'])
        for first,middle in [('R.','Martin'), ('W.','M.'), ('W.','Michael'), ('W.','')]:
            other, person, ctx = self.case(first,'William','Martin',middle)
            self.assertNotEqual(evaluate_assertion(other,person,ctx)[1]['identity_status'],'approved')
        _, spouse = evaluate_assertion({**record,'raw_salutation':'Deborah'},sec,context)
        self.assertEqual(spouse['email_greeting'],'Martin')

    def test_arbitrary_names_cannot_be_rescued_by_email(self):
        record, sec, context = self.case('Robert','William')
        self.assertNotEqual(evaluate_assertion(record,sec,context)[1]['identity_status'],'approved')

    def test_zack_is_a_strict_nontransitive_nickname(self):
        self.assertEqual(given_agreement(['Zack'],['Zachary']),(True,'given_strict_nickname'))
        self.assertFalse(given_agreement(['Zack'],['Zachariah'])[0])
        record, sec, context = self.case('Zack','Zachary')
        self.assertEqual(evaluate_assertion(record,sec,context)[1]['identity_status'],'approved')
        self.assertNotEqual(evaluate_assertion({**record,'crd_claim_count':2},sec,context)[1]['identity_status'],'approved')


class FieldAssets(unittest.TestCase):
    def test_whole_export_ignores_legacy_contact_and_team_balances(self):
        with tempfile.TemporaryDirectory() as folder:
            web=Path(folder)
            contacts={'advisors':{str(i):{'n':'Person '+str(i),'ia':999999,'tm':'legacy','t':'high'} for i in range(1,4)},
                      'teams':{'legacy':{'n':'Old team','a':888888,'sz':3}},'practices':{}}
            (web/'contacts.json').write_text(json.dumps(contacts),encoding='utf-8')
            (web/'advisor_index.json').write_text(json.dumps({'advisors':[[str(i),'Person '+str(i)] for i in range(1,4)]}),encoding='utf-8')
            book=self.book();book['advisor_index']={'sha256':core.file_hash(web/'advisor_index.json')}
            (web/'act_assets.json').write_text(json.dumps(book),encoding='utf-8')
            positions=pd.DataFrame([{'advisor_crd':str(i),'lat':40.0,'lon':-74.0,
                'branch_city':'Example City','branch_state':'NY','addr_key':'example'} for i in range(1,4)])
            with mock.patch.multiple(field_tiles,WEB=web,TILES=web/'tiles',PRACTICES=web/'practices'), \
                    mock.patch.object(field_tiles,'load_positions',return_value=positions), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                field_tiles.main()
                payload=json.loads(next((web/'tiles').glob('*.json')).read_text())
                amounts={r[0]:r[payload['columns'].index('assets')] for r in payload['rows']}
                self.assertEqual(amounts,{'1':128,'2':123,'3':0})
                self.assertIn('2 carrying an EIC asset figure',output.getvalue())
                book['advisor_index']['sha256']='stale'
                (web/'act_assets.json').write_text(json.dumps(book),encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'current advisor index'):field_tiles.main()

    def book(self):
        return {'columns':['acv','lcv','mf','midcap'], 'accounts':[[100,20,3,0],[5,0,0,0]],
            'advisors':{'1':{'t':'confirmed','ix':[0,1,0],'acv':999999},
                        '2':{'t':'high','ix':[0]}}}

    def test_shared_accounts_are_full_relationship_amounts_but_never_legacy_amounts(self):
        self.assertEqual(canonical_asset_amounts(self.book()), {'1':128,'2':123})
        self.assertNotIn('missing',canonical_asset_amounts(self.book()))

    def test_invalid_unapproved_stale_shape_is_not_silently_published(self):
        for tier,indices in [('review',[0]),('high',[-1]),('high',[True]),('high',[2]),('high',None)]:
            data=self.book();data['advisors']['1']={'t':tier,'ix':indices}
            with self.subTest(tier=tier,indices=indices), self.assertRaises(ValueError):
                canonical_asset_amounts(data)
        for value in [float('nan'),-1,True,'100',float('inf')]:
            data=self.book();data['accounts'][0][0]=value
            with self.subTest(value=value),self.assertRaises(ValueError):canonical_asset_amounts(data)


class StagingIO(unittest.TestCase):
    def test_baseline_prunes_only_scraper_scratch_and_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name in ['data/interim/firm_rosters/profile/Cookies','data/interim/firm_rosters/answers.json',
                         'data/interim/advisors.parquet','data/output/firms.parquet',
                         'data/identity/act_identity_links.parquet','webapp/data/pins_NY.json']:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('fixture')
            selected={p.relative_to(root).as_posix() for p in core.baseline_paths(root)}
            self.assertEqual(selected,{'data/interim/advisors.parquet','data/output/firms.parquet',
                'data/identity/act_identity_links.parquet','webapp/data/pins_NY.json'})

    def test_owned_quarantine_move_does_not_copy_and_can_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            stage=Path(folder); old=stage/'data/raw/old.json'; saved=stage/'data/refresh/quarantine/old.json'
            old.parent.mkdir(parents=True);old.write_bytes(b'last known good')
            with mock.patch.object(acquire.shutil,'copy2',side_effect=AssertionError('Must rename, not copy')):
                moved, expected=acquire._move_with_verification(stage,old,saved)
                self.assertTrue(moved);self.assertFalse(old.exists());self.assertEqual(core.file_hash(saved),expected)
                acquire._move_with_verification(stage,saved,old)
            self.assertEqual(old.read_bytes(),b'last known good')

    def test_failed_move_checksum_restores_source_and_outside_path_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            stage=Path(folder)/'stage';stage.mkdir(); old=stage/'old';old.write_bytes(b'good');saved=stage/'saved'
            with mock.patch.object(core,'file_hash',side_effect=['expected','mismatch']):
                with self.assertRaisesRegex(core.RefreshError,'checksum'):
                    acquire._move_with_verification(stage,old,saved)
            self.assertEqual(old.read_bytes(),b'good');self.assertFalse(saved.exists())
            with self.assertRaisesRegex(core.RefreshError,'escapes'):
                acquire._move_with_verification(stage,old,Path(folder)/'outside')

    def test_existing_quarantine_bytes_are_immutable(self):
        with tempfile.TemporaryDirectory() as folder:
            stage=Path(folder);old=stage/'old';saved=stage/'saved'
            old.write_bytes(b'good');saved.write_bytes(b'different')
            with self.assertRaisesRegex(core.RefreshError,'Immutable'):
                acquire._move_with_verification(stage,old,saved)
            self.assertEqual(old.read_bytes(),b'good');self.assertEqual(saved.read_bytes(),b'different')
            saved.write_bytes(b'good')
            moved,expected=acquire._move_with_verification(stage,old,saved)
            self.assertFalse(moved);self.assertFalse(old.exists());self.assertEqual(core.file_hash(saved),expected)
            acquire._copy(saved,old)
            self.assertEqual(old.read_bytes(),b'good');self.assertTrue(saved.exists())


if __name__ == '__main__':
    unittest.main()
