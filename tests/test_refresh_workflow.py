from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import refresh_core as core
import refresh_acquire as acquire
import refresh_workflow as workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'repo';self.root.mkdir()
        self.stage=Path(self.temp.name)/'stage';self.stage.mkdir()
        self.directory=core.run_path(self.root,'fixture');self.directory.mkdir(parents=True)
        core.atomic_json(self.stage/'.refresh-workspace.json',{'runId':'fixture','ownerToken':'fixture'})
        self.old=self.stage/'data/raw/firm_rosters/baird_2026-01-01.csv'
        self.old.parent.mkdir(parents=True);self.old.write_text('name,email\nAlice Smith,alice@example.invalid\n')
        self.manifest={'schemaVersion':1,'id':'fixture','ownerToken':'fixture','workspaceRoot':str(self.stage),
            'baselineRoot':str(self.root),'mode':'full','status':'ready','codeFiles':{},'codeNormalization':{},
            'inputs':core.facts(core.source_paths(self.stage),self.stage),'attempts':[],'imports':[],
            'sources':[{'id':'roster:baird','sourceDate':'2026-01-01','retrievedUtc':None}]}
        core.save_run(self.directory,self.manifest)

    def run_workflow(self,**options):
        options.setdefault('sources',['roster:baird']);options.setdefault('through','acquire')
        return workflow.run_workflow(self.root,'fixture',allow_network=True,**options)

    def success(self,root,run_id,sid,**options):
        _,manifest,stage=core.load_run(root,run_id)
        output=stage/('data/raw/firm_rosters/'+sid[7:]+'_2026-10-25.csv')
        output.write_text('name,email\nAlice Smith,new@example.invalid\n')
        manifest['inputs']=core.facts(core.source_paths(stage),stage)
        manifest['imports'].append({'sourceId':sid,'retrievedUtc':'2026-10-25T12:00:00Z'})
        manifest['attempts'].append({'sourceId':sid,'status':'success'})
        core.save_run(self.directory,manifest)
        return manifest

    def failure(self,root,run_id,sid,**options):
        _,manifest,_=core.load_run(root,run_id)
        manifest['attempts'].append({'sourceId':sid,'status':'failed','errorCode':'ACQUISITION_FAILED'})
        core.save_run(self.directory,manifest)
        raise core.RefreshError('Synthetic outage')

    def test_two_runs_resume_without_repeating_successful_acquisition(self):
        with patch.object(acquire,'acquire',side_effect=self.success) as call:
            first=self.run_workflow();second=self.run_workflow(sources=[])
        self.assertEqual('acquired',first['status']);self.assertEqual('acquired',second['status'])
        self.assertEqual(1,call.call_count)
        self.assertTrue((self.directory/'workflow/source_review.csv').exists())

    def test_actual_acquisition_subprocess_then_resume(self):
        # Exercise the real command runner, source import, historical comparison,
        # receipt, and workflow resume; the publisher itself is a synthetic CSV.
        script = self.stage / 'src/baird_async.py'
        script.parent.mkdir(parents=True)
        script.write_text("from pathlib import Path\n"
            "import csv\n"
            "p=Path('data/raw/firm_rosters/baird_2026-10-25.csv')\n"
            "p.parent.mkdir(parents=True,exist_ok=True)\n"
            "with p.open('w',newline='') as f:\n"
            " w=csv.DictWriter(f,fieldnames=['name','email','phone','city','state']);w.writeheader()\n"
            " for i in range(10):w.writerow(dict(name='Person '+str(i),email='person'+str(i)+'@example.invalid',phone='4045551234',city='Atlanta',state='GA'))\n")
        controller = self.root / 'src/baird_async.py';controller.parent.mkdir()
        controller.write_bytes(script.read_bytes())
        self.old.write_text('name,email,phone,city,state\n' + ''.join(
            'Person '+str(i)+',person'+str(i)+'@example.invalid,4045551234,Atlanta,GA\n'
            for i in range(10)))
        _, manifest, _ = core.load_run(self.root, 'fixture')
        manifest['codeFiles'] = core.facts([script], self.stage)
        manifest['codeNormalization'] = {'src/baird_async.py':core.normalized_code_hash(script, 'src/baird_async.py')}
        manifest['inputs'] = core.facts(core.source_paths(self.stage), self.stage)
        core.save_run(self.directory, manifest)
        first = self.run_workflow()
        self.assertEqual('acquired', first['status'])
        self.assertTrue(first['sources']['roster:baird']['retrievedUtc'])
        _, after, _ = core.load_run(self.root, 'fixture')
        self.assertEqual(1, len(after['attempts']))
        second = self.run_workflow(sources=[])
        self.assertEqual('acquired', second['status'])
        _, resumed, _ = core.load_run(self.root, 'fixture')
        self.assertEqual(1, len(resumed['attempts']))
        self.assertFalse((self.root / 'data/raw').exists())

    def test_outage_preserves_last_good_and_does_not_build(self):
        before=self.old.read_bytes()
        with patch.object(acquire,'acquire',side_effect=self.failure),patch.object(core,'build') as build:
            result=self.run_workflow(through='validate')
        self.assertEqual('needs_attention',result['status']);build.assert_not_called()
        self.assertEqual(before,self.old.read_bytes())
        self.assertIn('--retry-failed',result['sources']['roster:baird']['nextAction'])

    def test_only_explicit_per_source_reuse_allows_pipeline(self):
        with patch.object(acquire,'acquire',side_effect=self.failure), \
                patch.object(core,'build',return_value={'status':'built'}) as build, \
                patch('refresh_release.validate',return_value={'status':'validated'}) as validate:
            result=self.run_workflow(through='validate',reuse_last_good=['roster:baird'],
                fallback_reason='Reviewed outage; retain the existing Baird snapshot')
        self.assertEqual('completed_with_reuse',result['status'])
        self.assertEqual(1,build.call_count);self.assertEqual(1,validate.call_count)
        self.assertEqual('reused_last_good',result['sources']['roster:baird']['status'])
        self.assertIsNone(result['sources']['roster:baird']['retrievedUtc'])

    def test_interrupt_then_resume_retries_unfinished_source(self):
        with patch.object(acquire,'acquire',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.run_workflow()
        self.assertEqual('interrupted',core.read_json(self.directory/'workflow/state.json')['status'])
        with patch.object(acquire,'acquire',side_effect=self.success) as call:
            self.assertEqual('acquired',self.run_workflow(sources=[])['status'])
        self.assertEqual(1,call.call_count)

    def test_out_of_scope_fallback_and_source_plan_changes_are_rejected(self):
        with self.assertRaisesRegex(core.RefreshError,'outside selected'):
            self.run_workflow(reuse_last_good=['act'],fallback_reason='A sufficiently long review reason')
        with patch.object(acquire,'acquire',side_effect=self.success):self.run_workflow()
        with self.assertRaisesRegex(core.RefreshError,'plan cannot change'):
            self.run_workflow(sources=['roster:rbc'])

    def test_changed_selected_source_cannot_inherit_success(self):
        with patch.object(acquire,'acquire',side_effect=self.success):self.run_workflow()
        output=self.stage/'data/raw/firm_rosters/baird_2026-10-25.csv'
        output.write_text('different source bytes')
        _,manifest,_=core.load_run(self.root,'fixture')
        manifest['inputs']=core.facts(core.source_paths(self.stage),self.stage)
        core.save_run(self.directory,manifest)
        with self.assertRaisesRegex(core.RefreshError,'source changed'):
            self.run_workflow(sources=[])

    def test_no_network_permission_means_no_acquisition(self):
        with patch.object(acquire,'acquire') as call:
            with self.assertRaisesRegex(core.RefreshError,'allow-network'):
                workflow.run_workflow(self.root,'fixture',sources=['roster:baird'])
        call.assert_not_called()

    def test_failed_source_outside_workflow_cannot_receive_blanket_reuse(self):
        _,manifest,_=core.load_run(self.root,'fixture')
        manifest['attempts']=[{'sourceId':'act','status':'failed'}];core.save_run(self.directory,manifest)
        with patch.object(acquire,'acquire',side_effect=self.failure),patch.object(core,'build') as build:
            with self.assertRaisesRegex(core.RefreshError,'lack explicit'):
                self.run_workflow(through='validate',reuse_last_good=['roster:baird'],
                    fallback_reason='Reviewed Baird outage only; retain baseline')
        build.assert_not_called()

    def test_automatic_minor_policy_reuses_verified_roster_without_accepting_new_data(self):
        before=self.old.read_bytes()
        with patch.object(acquire,'acquire',side_effect=self.failure), \
                patch.object(core,'build',return_value={'status':'built'}) as build, \
                patch('refresh_release.validate',return_value={'status':'validated'}) as validate:
            result=self.run_workflow(through='validate',automatic_policy=True)
        self.assertEqual(result['status'],'completed_with_reuse')
        self.assertEqual(self.old.read_bytes(),before)
        self.assertEqual(result['sources']['roster:baird']['status'],'reused_last_good')
        self.assertIn('Automatic last-good source reuse: roster:baird',result['warnings'])
        self.assertTrue(validate.call_args.kwargs['minor_policy'])
        self.assertEqual(build.call_count,1)

    def test_automatic_policy_does_not_reuse_failed_act(self):
        with patch.object(acquire,'acquire',side_effect=self.failure),patch.object(core,'build') as build:
            result=self.run_workflow(sources=['act'],through='validate',automatic_policy=True)
        self.assertEqual(result['status'],'needs_attention');build.assert_not_called()


if __name__=='__main__':unittest.main()
