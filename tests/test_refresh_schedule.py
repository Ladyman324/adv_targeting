import datetime as dt
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import refresh_schedule as schedule


class ScheduleTests(unittest.TestCase):
    def plan(self, when, attempted=()):
        return schedule.make_plan(dt.datetime.fromisoformat(when), dt.date(2026, 11, 5), attempted)

    def test_weekly_act_only(self):
        p=self.plan('2026-11-07T03:00:00')
        self.assertEqual((p['mode'],p['sources'],p['runId']),('act-only',['act'],'scheduled-act-20261107'))
        self.assertFalse(p['publish']);self.assertTrue(p['runTests'])

    def test_quarterly_includes_act_once(self):
        for date in ('2027-01-02','2027-04-03','2027-07-03','2027-10-02'):
            p=self.plan(date+'T03:00:00')
            self.assertEqual(p['mode'],'full');self.assertEqual(p['sources'],'all')

    def test_before_three_am_uses_previous_slot(self):
        self.assertEqual(self.plan('2027-01-02T02:59:59')['scheduledDate'],'2026-12-26')

    def test_delayed_start_keeps_quarter_slot(self):
        p=self.plan('2027-01-05T11:00:00')
        self.assertEqual(p['runId'],'scheduled-full-20270102')

    def test_missed_quarter_caught_up_after_a_week(self):
        self.assertEqual(self.plan('2027-01-16T11:00:00')['runId'],'scheduled-full-20270102')

    def test_held_quarter_does_not_block_following_week_act(self):
        p=self.plan('2027-01-09T03:00:00',('2027-01-02',))
        self.assertEqual(p['mode'],'act-only')

    def test_same_slot_resume_has_same_identity(self):
        a=self.plan('2027-01-02T03:00:00');b=self.plan('2027-01-02T04:00:00',('2027-01-02',))
        self.assertEqual(a['runId'],b['runId'])

    def test_existing_environment_does_not_query_azure(self):
        def fail(*a,**k):self.fail('Unexpected credential lookup')
        values=dict(ACT_USER='u',ACT_DB='d',ACT_PASSWORD='private-test-value')
        self.assertEqual(schedule.act_credentials(values,fail),values)

    @patch('refresh_schedule.shutil.which',return_value='az.cmd')
    def test_failed_lookup_does_not_expose_output(self,_):
        def run(*a,**k):return SimpleNamespace(returncode=1,stdout='private-test-value',stderr='private-test-value')
        with self.assertRaises(RuntimeError) as caught:schedule.act_credentials({},run)
        self.assertNotIn('private-test-value',str(caught.exception))

    @patch('refresh_schedule.shutil.which',return_value='az.cmd')
    def test_credential_endpoint_guard(self,_):
        values=dict(ACT_USER='u',ACT_DB='d',ACT_PASSWORD='private-test-value',ACT_BASE='https://example.invalid')
        def run(*a,**k):return SimpleNamespace(returncode=0,stdout=json.dumps([dict(name=k,value=v) for k,v in values.items()]))
        with self.assertRaisesRegex(RuntimeError,'endpoint requires review'):schedule.act_credentials({},run)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.stage=self.root/'workspaces'
        self.registration=self.root/'registration.json'
        self.registration.write_text(json.dumps(dict(timezone='Eastern Standard Time',activeFrom='2026-11-05')),encoding='utf-8-sig')

    def test_windows_registration_and_duplicate_run(self):
        import os
        original=os.environ.get('ACT_PASSWORD')
        credentials=dict(ACT_USER='u',ACT_DB='d',ACT_PASSWORD='private-test-value')
        def workflow(*args,**kwargs):
            self.assertEqual(os.environ['ACT_PASSWORD'],'private-test-value')
            self.assertEqual(kwargs['through'],'prepare-release')
            self.assertEqual(kwargs['sources'],('act',))
            self.assertTrue(kwargs['allow_network'])
            return dict(status='completed')
        with patch('refresh_schedule.act_credentials',return_value=credentials.copy()),patch('refresh_workflow.run_workflow',side_effect=workflow) as run:
            a=schedule.execute(self.root,self.stage,self.registration,dt.datetime(2026,11,7,3))
            b=schedule.execute(self.root,self.stage,self.registration,dt.datetime(2026,11,7,4))
        self.assertEqual(a['status'],'completed');self.assertEqual(b['status'],'already_completed')
        self.assertEqual(run.call_count,1);self.assertEqual(os.environ.get('ACT_PASSWORD'),original)
        self.assertNotIn('private-test-value',(self.root/'data/refresh/scheduled/history.json').read_text())

    def test_failure_records_hold_and_restores_environment(self):
        import os
        original=os.environ.get('ACT_PASSWORD')
        credentials=dict(ACT_USER='u',ACT_DB='d',ACT_PASSWORD='private-test-value')
        with patch('refresh_schedule.act_credentials',return_value=credentials.copy()),patch('refresh_workflow.run_workflow',side_effect=RuntimeError('fixture failure')):
            with self.assertRaisesRegex(RuntimeError,'fixture failure'):
                schedule.execute(self.root,self.stage,self.registration,dt.datetime(2027,1,2,3))
        history=json.loads((self.root/'data/refresh/scheduled/history.json').read_text())
        self.assertEqual(history['quarterlyAttempts'],['2027-01-02'])
        self.assertEqual(history['runs']['scheduled-full-20270102']['status'],'needs_attention')
        self.assertEqual(os.environ.get('ACT_PASSWORD'),original)


if __name__=='__main__':unittest.main()
