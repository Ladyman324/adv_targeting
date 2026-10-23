"""Durable staging can recover interruptions without recopying live data."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import refresh_core as core
import refresh_inventory
import refresh_stage as staging


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root, self.stages = self.base / 'repo', self.base / 'stages'
        self.root.mkdir()
        self.stages.mkdir()
        self.write('src/job.py', 'print(1)')
        self.write('data/raw/source.json', '[1]')
        self.write('webapp/data/baseline.json', '{"original":true}')
        self.write('api/shared/act_mail_codes.json', '{"addresses":{}}')

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='utf-8')
        return path

    @contextlib.contextmanager
    def fixtures(self):
        with mock.patch.object(refresh_inventory, 'inventory', return_value=[]), \
             mock.patch.object(refresh_inventory, 'validate_act_snapshot', return_value={}), \
             mock.patch.object(core.subprocess, 'check_output', side_effect=['a' * 40, '']):
            yield

    def start_interrupted(self, target='_finish'):
        with self.fixtures(), mock.patch.object(staging, target, side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                staging.start_stage(self.root, 'act-only', stage_parent=self.stages, run_id='test')
        self.directory = self.root / 'data/refresh/runs/test'
        manifest = core.read_json(self.directory / 'run.json')
        self.stage = Path(manifest['workspaceRoot'])
        return manifest

    def resume(self):
        with self.fixtures():
            return staging.resume_stage(self.root, 'test')

    def test_interruption_before_plan_leaves_durable_resumable_bootstrap(self):
        first = self.start_interrupted('_capture')
        self.assertEqual(first['phase'], 'capture')
        self.assertFalse(first['snapshotComplete'])
        self.assertEqual(first['status'], 'incomplete')
        self.assertEqual(first['outcome'], 'incomplete')
        # No snapshot had been registered; explicit reselection is recorded.
        self.write('data/raw/source.json', '[2]')
        result = self.resume()
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['outcome'], 'success_with_warnings')
        self.assertTrue(any('reselecting' in warning for warning in result['warnings']))
        self.assertEqual((self.stage / 'data/raw/source.json').read_text(), '[2]')

    def test_partial_copies_repaired_and_verified_without_production_changes(self):
        first = self.start_interrupted()
        self.assertEqual(first['phase'], 'copy')
        self.assertTrue(first['snapshotComplete'])
        partial = self.stage / 'src/job.py'
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_text('partial', encoding='utf-8')
        baseline_bytes = (self.root / 'webapp/data/baseline.json').read_bytes()
        result = self.resume()
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(partial.read_bytes(), (self.root / 'src/job.py').read_bytes())
        self.assertEqual((self.root / 'webapp/data/baseline.json').read_bytes(), baseline_bytes)
        for relative, fact in result['copyPlan'].items():
            self.assertEqual(core.file_hash(self.stage / relative), fact['sha256'])
        self.assertTrue((self.stage / 'data/refresh/supporting_inputs/act_mail_codes.json').is_file())

    def test_registered_source_drift_refuses_resume(self):
        self.start_interrupted()
        self.write('data/raw/source.json', '[2]')
        with self.assertRaisesRegex(core.RefreshError, 'source inputs changed'):
            self.resume()
        self.assertFalse((self.stage / 'src/job.py').exists())
        self.assertEqual(core.read_json(self.directory / 'run.json')['outcome'], 'failed')

    def test_original_verification_hashes_each_input_only_once(self):
        manifest = self.start_interrupted()
        with mock.patch.object(core, 'facts', wraps=core.facts) as hashing:
            staging._verify_originals(core, self.root, manifest)
        self.assertEqual(hashing.call_count, 1)
        args = hashing.call_args.args
        self.assertEqual(len(args[0]), len(manifest['copyPlan']))
        self.assertEqual(set(args[0]), {self.root / p for p in manifest['copyPlan']})

    def test_removed_recorded_file_detected_before_hashing(self):
        manifest = self.start_interrupted()
        (self.root / 'data/raw/source.json').unlink()
        with mock.patch.object(core, 'facts', wraps=core.facts) as hashing:
            with self.assertRaisesRegex(core.RefreshError, 'source inputs changed'):
                staging._verify_originals(core, self.root, manifest)
        self.assertEqual(hashing.call_count, 0)

    def test_controller_code_drift_refuses_resume(self):
        self.start_interrupted()
        self.write('src/job.py', 'print(2)')
        with self.assertRaisesRegex(core.RefreshError, 'controller code changed'):
            self.resume()

    def test_baseline_drift_refuses_resume(self):
        self.start_interrupted()
        self.write('webapp/data/baseline.json', '{}')
        with self.assertRaisesRegex(core.RefreshError, 'baseline artifacts changed'):
            self.resume()

    def test_new_source_file_is_not_silently_accepted(self):
        self.start_interrupted()
        self.write('data/raw/new.json', '{}')
        with self.assertRaisesRegex(core.RefreshError, 'source inputs changed'):
            self.resume()

    def test_new_code_file_is_not_silently_accepted(self):
        self.start_interrupted()
        self.write('src/new.py', 'print(2)')
        with self.assertRaisesRegex(core.RefreshError, 'controller code changed'):
            self.resume()

    def test_interruption_during_real_copy_repairs_partial_temporary_file(self):
        actual_copy = staging._copy_file
        def fail_one(module, source, destination, fact):
            if source.name == 'job.py':
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.with_name(destination.name + '.refresh-copy.tmp').write_text('partial')
                raise KeyboardInterrupt()
            return actual_copy(module, source, destination, fact)
        with self.fixtures(), mock.patch.object(staging, '_copy_file', side_effect=fail_one):
            with self.assertRaises(KeyboardInterrupt):
                staging.start_stage(self.root, 'act-only', stage_parent=self.stages, run_id='test')
        self.directory = self.root / 'data/refresh/runs/test'
        manifest = core.read_json(self.directory / 'run.json')
        self.stage = Path(manifest['workspaceRoot'])
        self.assertEqual(manifest['status'], 'incomplete')
        self.assertTrue(manifest['snapshotComplete'])
        self.assertEqual(self.resume()['status'], 'ready')
        self.assertEqual((self.stage / 'src/job.py').read_bytes(), (self.root / 'src/job.py').read_bytes())
        self.assertFalse((self.stage / 'src/job.py.refresh-copy.tmp').exists())

    def test_concurrent_resume_rejected_by_run_lock(self):
        self.start_interrupted()
        with core.lock(self.directory):
            with self.assertRaisesRegex(core.RefreshError, 'owns this refresh run'):
                self.resume()

    def test_copy_plan_tampering_is_detected(self):
        manifest = self.start_interrupted()
        manifest['copyPlan']['src/job.py']['sha256'] = '0' * 64
        core.atomic_json(self.directory / 'run.json', manifest)
        with self.assertRaisesRegex(core.RefreshError, 'integrity'):
            self.resume()

    def test_staging_cannot_recopy_after_build_has_started(self):
        manifest = self.start_interrupted()
        manifest['steps'] = {'one': {'status': 'running'}}
        core.atomic_json(self.directory / 'run.json', manifest)
        with self.assertRaisesRegex(core.RefreshError, 'building'):
            self.resume()

    def test_ready_cannot_be_recopied_by_staging_resume(self):
        self.start_interrupted()
        self.resume()
        with self.assertRaisesRegex(core.RefreshError, 'ready'):
            self.resume()

    def test_workspace_ownership_is_verified(self):
        self.start_interrupted()
        core.atomic_json(self.stage / '.refresh-workspace.json', {'runId': 'other', 'ownerToken': 'wrong'})
        with self.assertRaisesRegex(core.RefreshError, 'ownership'):
            self.resume()

    def test_ranking_seed_snapshot_and_suppression_floor_survive_recovery(self):
        self.write('data/raw/barrons_rankings.json', '[{"year":2026}]')
        self.start_interrupted()
        result = self.resume()
        relative, fact = next(iter(result['legacyRankingFacts'].items()))
        seed = self.stage / 'data/refresh/ranking_editions/barrons' / (fact['sha256'] + '.json')
        self.assertEqual(core.file_hash(seed), fact['sha256'])
        self.assertEqual(core.file_hash(self.stage / 'data/refresh/supporting_inputs/act_mail_codes.json'),
                         core.file_hash(self.root / 'api/shared/act_mail_codes.json'))

    def assert_stage_injection_rejected(self, relative):
        self.start_interrupted()
        path = self.stage / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(core.RefreshError, 'Unregistered staged file'):
            self.resume()
        self.assertEqual(core.read_json(self.directory / 'run.json')['outcome'], 'failed')

    def test_unregistered_raw_input_cannot_be_adopted_on_resume(self):
        self.assert_stage_injection_rejected('data/raw/unregistered.json')

    def test_injected_roster_selector_cannot_be_adopted_on_resume(self):
        self.assert_stage_injection_rejected('data/raw/firm_rosters/ubs_2099-01-01.csv')

    def test_injected_act_selector_cannot_be_adopted_on_resume(self):
        self.assert_stage_injection_rejected('data/raw/act_contacts_2099-01-01T000000Z.json')

    def test_injected_code_cannot_be_adopted_on_resume(self):
        self.assert_stage_injection_rejected('src/injected.py')

    def test_unexpected_ranking_history_is_not_a_generated_target(self):
        self.assert_stage_injection_rejected('data/refresh/ranking_editions/barrons/unregistered.json')

    def test_unexpected_temporary_is_not_a_copy_recovery_file(self):
        self.assert_stage_injection_rejected('data/raw/unregistered.json.refresh-copy.tmp')

    def test_stale_expected_temporary_removed_before_input_registration(self):
        self.start_interrupted()
        target = self.stage / 'data/raw/source.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((self.root / 'data/raw/source.json').read_bytes())
        temporary = target.with_name(target.name + '.refresh-copy.tmp')
        temporary.write_text('partial', encoding='utf-8')
        result = self.resume()
        self.assertEqual(result['status'], 'ready')
        self.assertFalse(temporary.exists())
        self.assertNotIn('data/raw/source.json.refresh-copy.tmp', result['inputs'])


if __name__ == '__main__':
    unittest.main()
