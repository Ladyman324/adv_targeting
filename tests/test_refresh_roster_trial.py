from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import refresh_core as core
import refresh_roster_trial as trial


class RosterTrialTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)/'repo';self.root.mkdir()
        self.work=Path(temp.name)/'worker';self.work.mkdir()
        script=self.root/'src/baird_async.py';script.parent.mkdir()
        self.script=script
        script.write_text("from pathlib import Path\np=Path('data/raw/firm_rosters/baird_2026-10-25.csv')\n"
            "p.write_text('name,email,phone,city,state\\n'+''.join('Person '+str(i)+',person'+str(i)+'@example.invalid,4045551234,Atlanta,GA\\n' for i in range(10)))\n")
        baseline=self.root/'data/raw/firm_rosters/baird_2026-09-01.csv'
        baseline.parent.mkdir(parents=True)
        baseline.write_text('name,email,phone,city,state\n'+''.join(
            'Person '+str(i)+',person'+str(i)+'@example.invalid,4045551234,Atlanta,GA\n' for i in range(10)))
        self.baseline=baseline;self.before=baseline.read_bytes()

    def test_real_subprocess_retains_baseline_and_records_hash_bound_fresh_candidate(self):
        with patch.object(trial.tempfile,'mkdtemp',return_value=str(self.work)):
            result=trial.run_trial(self.root,'fixture','baird',allow_network=True)
        self.assertEqual('accepted',result['status'])
        folder=self.root/'data/refresh/roster_trials/fixture'
        self.assertEqual(result['sha256'],core.file_hash(folder/result['candidate']))
        self.assertTrue(result['retrievedUtc'])
        self.assertEqual(self.before,self.baseline.read_bytes())
        self.assertFalse((self.root/'data/raw/firm_rosters/baird_2026-10-25.csv').exists())
        with self.assertRaisesRegex(core.RefreshError,'already exists'):
            trial.run_trial(self.root,'fixture','baird',allow_network=True)

    def test_failed_scraper_never_selects_old_roster_as_fresh(self):
        self.script.write_text('raise RuntimeError("Synthetic source outage")')
        with patch.object(trial.tempfile,'mkdtemp',return_value=str(self.work)):
            with self.assertRaises(core.RefreshError):trial.run_trial(self.root,'fixture','baird',allow_network=True)
        folder=self.root/'data/refresh/roster_trials/fixture'
        self.assertEqual('failed',core.read_json(folder/'trial.json')['status'])
        self.assertFalse((folder/'candidates').exists())
        self.assertEqual(self.before,self.baseline.read_bytes())

    def test_no_network_authority_and_bad_run_id_are_rejected_before_staging(self):
        with self.assertRaises(core.RefreshError):trial.run_trial(self.root,'fixture','baird')
        with self.assertRaises(core.RefreshError):trial.run_trial(self.root,'../escape','baird',allow_network=True)
        self.assertFalse((self.root/'data/refresh').exists())
