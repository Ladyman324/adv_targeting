import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from raymond_checkpoint import advisor_csv_path, seal_file, prepare_file_write
import raymond_checkpoint as checkpoint


class RaymondCheckpointTests(unittest.TestCase):
    def test_unpublished_fork_rejected_before_creating_normal_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / checkpoint.FORK_INTENT).write_text('{}')
            with self.assertRaisesRegex(ValueError, 'unpublished'):
                self.invoke(root)
            self.assertFalse((root / 'raymond_james_scope.json').exists())

    def test_fork_validation_precedes_pending_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.invoke(tmp)
            with patch.object(checkpoint, 'validate_reviewed_fork', side_effect=ValueError('invalid ancestry')):
                with patch.object(checkpoint, '_recover_pending') as recover:
                    with self.assertRaisesRegex(ValueError, 'invalid ancestry'):
                        self.invoke(tmp)
                    recover.assert_not_called()

    def invoke(self, root, name='raymond_james_2026-10-26.csv', **kwargs):
        return advisor_csv_path(root, proposed_name=name, radius=kwargs.pop('radius', 100),
            collector_path=Path(__file__), **kwargs)

    def test_next_day_keeps_original_data_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.invoke(tmp, fresh=True)
            self.assertEqual(first, self.invoke(tmp, name='raymond_james_2026-10-27.csv'))

    def test_fresh_cannot_destroy_existing_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.invoke(tmp)
            with self.assertRaises(ValueError): self.invoke(tmp, fresh=True)

    def test_radius_change_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.invoke(tmp)
            with self.assertRaises(ValueError): self.invoke(tmp, radius=50)

    def test_missing_completed_city_data_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.invoke(tmp)
            (Path(tmp) / 'raymond_james_completed_locations.json').write_text('["Boston, MA"]')
            with self.assertRaises(ValueError): self.invoke(tmp)

    def test_legacy_state_not_silently_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'raymond_james_completed_locations.json').write_text('[]')
            with self.assertRaises(ValueError): self.invoke(tmp)

    def test_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.invoke(tmp)
            manifest = Path(tmp) / 'raymond_james_scope.json'
            scope = json.loads(manifest.read_text())
            scope['csvName'] = '../escape.csv'
            manifest.write_text(json.dumps(scope))
            with self.assertRaises(ValueError): self.invoke(tmp)

    def test_changed_csv_bytes_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.invoke(tmp)
            path.write_text('name\nOriginal row\n')
            seal_file(path)
            self.assertEqual(path, self.invoke(tmp))
            path.write_text('name\nOnly surviving row\n')
            with self.assertRaises(ValueError): self.invoke(tmp)

    def test_completed_list_is_bound_to_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.invoke(tmp)
            path.write_text('name\nOriginal row\n')
            seal_file(path)
            completed = Path(tmp) / 'raymond_james_completed_locations.json'
            completed.write_text('["Boston, MA"]')
            seal_file(completed)
            self.assertEqual(path, self.invoke(tmp))
            completed.write_text('["Boston, MA", "New York, NY"]')
            with self.assertRaises(ValueError): self.invoke(tmp)

    def test_unsealed_write_is_not_silently_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.invoke(tmp)
            path.write_text('interrupted new data')
            with self.assertRaises(ValueError): self.invoke(tmp)

    def seed_pair(self,root):
        root=Path(root);path=self.invoke(root)
        path.write_bytes(b'name\nOld advisor\n');seal_file(path)
        completed=root/'raymond_james_completed_locations.json'
        completed.write_text('["Boston, MA"]');seal_file(completed)
        return path,completed

    def manifest(self,root):
        return json.loads((Path(root)/'raymond_james_scope.json').read_text())

    def archived_bytes(self,root):
        return [path.read_bytes() for path in (Path(root)/'raymond_james_recovery').rglob('*.uncommitted')]

    def test_prepared_commit_snapshots_and_clears_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path,_=self.seed_pair(tmp);old=path.read_bytes()
            prepare_file_write(path);self.assertEqual(path.name,self.manifest(tmp)['pendingWrite']['name'])
            path.write_bytes(b'name\nNew advisor\n');seal_file(path)
            scope=self.manifest(tmp)
            self.assertNotIn('pendingWrite',scope)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),scope['csvSha256'])
            self.assertEqual(old,checkpoint._snapshot_path(Path(tmp),hashlib.sha256(old).hexdigest()).read_bytes())
            self.assertEqual(path,self.invoke(tmp))

    def test_crash_before_replace_preserves_temp_and_keeps_committed_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            path,completed=self.seed_pair(tmp);old=path.read_bytes();old_completed=completed.read_bytes()
            prepare_file_write(path)
            path.with_suffix('.csv.tmp').write_bytes(b'name\nPartially written')
            self.invoke(tmp)
            self.assertEqual(old,path.read_bytes());self.assertEqual(old_completed,completed.read_bytes())
            self.assertIn(b'name\nPartially written',self.archived_bytes(tmp))
            self.assertNotIn('pendingWrite',self.manifest(tmp))

    def test_crash_after_csv_replace_restores_committed_pair_and_archives_new_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path,completed=self.seed_pair(tmp);old=path.read_bytes();old_completed=completed.read_bytes()
            prepare_file_write(path);path.write_bytes(b'name\nUncommitted advisor\n')
            self.invoke(tmp)
            self.assertEqual(old,path.read_bytes());self.assertEqual(old_completed,completed.read_bytes())
            self.assertIn(b'name\nUncommitted advisor\n',self.archived_bytes(tmp))

    def test_crash_during_completed_write_restores_previous_cities_but_keeps_new_committed_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path,completed=self.seed_pair(tmp);old_completed=completed.read_bytes()
            prepare_file_write(path);path.write_bytes(b'name\nOld advisor\nNew advisor\n');seal_file(path)
            new=path.read_bytes()
            prepare_file_write(completed);completed.write_text('["Boston, MA", "New York, NY"]')
            self.invoke(tmp)
            self.assertEqual(new,path.read_bytes());self.assertEqual(old_completed,completed.read_bytes())
            self.assertIn(b'["Boston, MA", "New York, NY"]',self.archived_bytes(tmp))

    def test_crash_on_initial_write_moves_uncommitted_file_to_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=self.invoke(tmp)
            prepare_file_write(path);path.write_bytes(b'name\nFirst uncommitted advisor\n')
            self.invoke(tmp)
            self.assertFalse(path.exists())
            self.assertIn(b'name\nFirst uncommitted advisor\n',self.archived_bytes(tmp))
            self.assertNotIn('pendingWrite',self.manifest(tmp))

    def test_snapshot_created_but_manifest_commit_interrupted_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path,_=self.seed_pair(tmp);old=path.read_bytes()
            prepare_file_write(path);path.write_bytes(b'name\nNew uncommitted advisor\n')
            original_save=checkpoint._save
            def fail_manifest(target,value):
                if target.name=='raymond_james_scope.json':raise OSError('simulated exit before manifest commit')
                return original_save(target,value)
            with patch.object(checkpoint,'_save',side_effect=fail_manifest),self.assertRaises(OSError):
                seal_file(path)
            self.assertTrue(checkpoint._snapshot_path(Path(tmp),hashlib.sha256(path.read_bytes()).hexdigest()).exists())
            self.invoke(tmp);self.assertEqual(old,path.read_bytes())
            self.assertIn(b'name\nNew uncommitted advisor\n',self.archived_bytes(tmp))

    def test_recovery_interrupted_after_restore_is_idempotent_and_preserves_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path,_=self.seed_pair(tmp);old=path.read_bytes()
            prepare_file_write(path);path.write_bytes(b'name\nUncommitted advisor\n')
            original_save=checkpoint._save
            def fail_manifest(target,value):
                if target.name=='raymond_james_scope.json':raise OSError('simulated exit after restoration')
                return original_save(target,value)
            with patch.object(checkpoint,'_save',side_effect=fail_manifest),self.assertRaises(OSError):
                self.invoke(tmp)
            self.assertEqual(old,path.read_bytes());self.assertIn('pendingWrite',self.manifest(tmp))
            self.invoke(tmp);self.assertEqual(old,path.read_bytes())
            receipt=Path(tmp)/self.manifest(tmp)['lastRecovery']
            records=json.loads(receipt.read_text())['archived']
            self.assertTrue(records)
            self.assertIn(b'name\nUncommitted advisor\n',self.archived_bytes(tmp))

    def test_corrupt_committed_snapshot_never_restores_or_changes_working_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path,_=self.seed_pair(tmp)
            prepare_file_write(path);path.write_bytes(b'name\nUncommitted advisor\n')
            scope=self.manifest(tmp);manifest_before=(Path(tmp)/'raymond_james_scope.json').read_bytes()
            checkpoint._snapshot_path(Path(tmp),scope['csvSha256']).write_bytes(b'corrupted')
            with self.assertRaises(ValueError):self.invoke(tmp)
            self.assertEqual(b'name\nUncommitted advisor\n',path.read_bytes())
            self.assertEqual(manifest_before,(Path(tmp)/'raymond_james_scope.json').read_bytes())
            self.assertFalse((Path(tmp)/'raymond_james_recovery').exists())

    def test_pending_intent_does_not_authorize_recovery_of_other_file_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            path,completed=self.seed_pair(tmp)
            prepare_file_write(path);path.write_bytes(b'name\nUncommitted advisor\n')
            completed.write_text('["Unexpected city"]')
            with self.assertRaises(ValueError):self.invoke(tmp)
            self.assertEqual('["Unexpected city"]',completed.read_text())
            self.assertFalse((Path(tmp)/'raymond_james_recovery').exists())

    def test_missing_working_file_during_intended_write_restores_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path,_=self.seed_pair(tmp);old=path.read_bytes();prepare_file_write(path)
            path.rename(path.with_suffix('.lost'))
            self.invoke(tmp);self.assertEqual(old,path.read_bytes())

    def test_nested_write_intents_rejected_until_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path,completed=self.seed_pair(tmp);prepare_file_write(path)
            with self.assertRaises(ValueError):prepare_file_write(completed)
            self.invoke(tmp)
            prepare_file_write(completed)
            self.assertEqual(completed.name,self.manifest(tmp)['pendingWrite']['name'])

    def test_interrupted_recovery_cannot_discard_missing_archived_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path,_=self.seed_pair(tmp)
            prepare_file_write(path);path.write_bytes(b'name\nUncommitted advisor\n')
            original_save=checkpoint._save
            def fail_manifest(target,value):
                if target.name=='raymond_james_scope.json':raise OSError('simulated recovery exit')
                return original_save(target,value)
            with patch.object(checkpoint,'_save',side_effect=fail_manifest),self.assertRaises(OSError):
                self.invoke(tmp)
            archived=next((Path(tmp)/'raymond_james_recovery').rglob('*.uncommitted'))
            archived.rename(archived.with_suffix('.removed'))
            with self.assertRaises(ValueError):self.invoke(tmp)
            self.assertIn('pendingWrite',self.manifest(tmp))

    def test_invalid_completed_city_data_is_not_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _,completed=self.seed_pair(tmp);old_hash=self.manifest(tmp)['completedSha256']
            prepare_file_write(completed);completed.write_text('{"not":"a city list"}')
            with self.assertRaises(ValueError):seal_file(completed)
            self.assertEqual(old_hash,self.manifest(tmp)['completedSha256'])
            self.invoke(tmp)
            self.assertEqual('["Boston, MA"]',completed.read_text())
