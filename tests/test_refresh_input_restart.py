"""Fresh code can reuse verified source inputs without inheriting old success."""
import contextlib,pathlib,sys,tempfile,unittest
from unittest import mock
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/"src"))
import refresh_core as core
import refresh_stage as stage
import refresh_inventory

class InputRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=pathlib.Path(self.tmp.name);self.root=self.base/"repo";self.stages=self.base/"stages"
        self.root.mkdir();self.stages.mkdir()
        for rel,text in [("src/job.py","print('old')"),("data/raw/source.json","[1]"),("webapp/data/baseline.json","{}")]:
            p=self.root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(refresh_inventory,"inventory",return_value=[]))
        self.stack.enter_context(mock.patch.object(refresh_inventory,"validate_act_snapshot",return_value={}))
        self.stack.enter_context(mock.patch.object(core.subprocess,"check_output",side_effect=lambda args,**kw:"a"*40 if "rev-parse" in args else ""))
        self.prior=core.create_run(self.root,"full",stage_parent=self.stages,run_id="prior")
        self.input_stage=pathlib.Path(self.prior["workspaceRoot"])
        (self.input_stage/"data/raw/source.json").write_text("[2]")
        self.prior["inputs"]=core.facts(core.source_paths(self.input_stage,"full"),self.input_stage)
        self.prior["steps"]={"old":{"status":"success"}};self.prior["validation"]={"status":"success"}
        self.prior["attempts"]=[{"sourceId":"roster:held","status":"failed"}]
        core.save_run(core.run_path(self.root,"prior"),self.prior)
        (self.root/"src/job.py").write_text("print('new')")

    def start(self,**kwargs):
        return core.create_run(self.root,"full",stage_parent=self.stages,run_id="new",source_run="prior",**kwargs)

    def test_current_code_and_selected_inputs_have_new_success_state(self):
        result=self.start();target=pathlib.Path(result["workspaceRoot"])
        self.assertEqual("[2]",(target/"data/raw/source.json").read_text())
        self.assertEqual("print('new')",(target/"src/job.py").read_text())
        self.assertEqual({},result["steps"]);self.assertEqual([],result["imports"])
        self.assertNotIn("validation",result)
        self.assertTrue(any("roster:held" in warning for warning in result["warnings"]))
        core.check_inputs(target,result)

    def test_tampered_parent_input_refuses_new_run(self):
        (self.input_stage/"data/raw/source.json").write_text("tampered")
        with self.assertRaisesRegex(core.RefreshError,"Inherited source inputs changed"):self.start()

    def test_active_parent_cannot_be_used(self):
        with mock.patch.object(core,"active_children",return_value=[{"pid":123}]):
            with self.assertRaisesRegex(core.RefreshError,"must be idle"):self.start()

    def test_scope_mismatch_is_rejected(self):
        with self.assertRaisesRegex(core.RefreshError,"matching scope"):
            core.create_run(self.root,"act-only",stage_parent=self.stages,run_id="new",source_run="prior")

    def test_parent_manifest_drift_refuses_interrupted_copy_resume(self):
        with mock.patch.object(stage,"_finish",side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.start()
        self.prior["note"]="changed after capture";core.save_run(core.run_path(self.root,"prior"),self.prior)
        with self.assertRaisesRegex(core.RefreshError,"Inherited source snapshot changed"):
            stage.resume_stage(self.root,"new")

    def test_source_drift_during_copy_cannot_finish_staging(self):
        original=stage._copy_file
        def copy_then_change(module,source,destination,fact):
            original(module,source,destination,fact)
            if source==self.input_stage/"data/raw/source.json":source.write_text("changed during copy")
        with mock.patch.object(stage,"_copy_file",side_effect=copy_then_change):
            with self.assertRaisesRegex(core.RefreshError,"source inputs changed"):self.start()
        result=core.read_json(core.run_path(self.root,"new")/"run.json")
        self.assertEqual("failed",result["status"])
        self.assertEqual({},result["steps"])

    def test_source_restart_dry_run_is_read_only(self):
        import io,json,refresh_data
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0,refresh_data.main(["--root",str(self.root),"start","--mode","full","--source-run","prior","--dry-run"]))
        result=json.loads(output.getvalue())
        self.assertTrue(result["readOnly"]);self.assertEqual("prior",result["sourceRun"])
        self.assertEqual(["prior"],sorted(p.name for p in (self.root/"data/refresh/runs").iterdir()))

    def clean_root(self):
        root=self.base/"clean-release"
        (root/"src").mkdir(parents=True)
        (root/"src/job.py").write_text("print('clean committed release')")
        return root

    def test_cross_checkout_reuses_only_verified_inputs_with_current_code(self):
        clean=self.clean_root()
        policy=clean/"data/identity/roster_identity_holds.json";policy.parent.mkdir(parents=True)
        policy.write_text('{"schemaVersion":1,"holds":[]}')
        result=core.create_run(clean,"full",stage_parent=self.stages,run_id="new",source_run="prior",source_run_root=self.root)
        target=pathlib.Path(result["workspaceRoot"])
        self.assertEqual("[2]",(target/"data/raw/source.json").read_text())
        self.assertEqual("print('clean committed release')",(target/"src/job.py").read_text())
        self.assertEqual(str(self.root.resolve()),result["sourceSnapshot"]["baselineRoot"])
        self.assertIn("data/identity/roster_identity_holds.json",result["inputs"])
        self.assertEqual({},result["steps"]);self.assertNotIn("validation",result)
        core.check_inputs(target,result)

    def test_cross_checkout_parent_manifest_drift_refuses_resume(self):
        clean=self.clean_root()
        with mock.patch.object(stage,"_finish",side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                core.create_run(clean,"full",stage_parent=self.stages,run_id="new",source_run="prior",source_run_root=self.root)
        self.prior["note"]="changed original parent";core.save_run(core.run_path(self.root,"prior"),self.prior)
        with self.assertRaisesRegex(core.RefreshError,"Inherited source snapshot changed"):
            stage.resume_stage(clean,"new")

    def test_cross_checkout_active_source_owner_is_rejected(self):
        clean=self.clean_root();parent=core.run_path(self.root,"prior")
        with mock.patch.object(core,"active_children",side_effect=lambda p:[{"pid":123}] if pathlib.Path(p).resolve()==parent.resolve() else []):
            with self.assertRaisesRegex(core.RefreshError,"must be idle"):
                core.create_run(clean,"full",stage_parent=self.stages,run_id="new",source_run="prior",source_run_root=self.root)

    def test_source_owner_argument_cannot_be_used_without_a_source_run(self):
        with self.assertRaisesRegex(core.RefreshError,"requires a source run"):
            core.create_run(self.root,"full",stage_parent=self.stages,run_id="new",source_run_root=self.root)
        import refresh_data
        with self.assertRaisesRegex(core.RefreshError,"requires a source run"):
            refresh_data.main(["--root",str(self.root),"start","--mode","full","--source-run-root",str(self.root),"--dry-run"])

    def test_cross_checkout_dry_run_records_owner_without_creating_a_run(self):
        import io,json,refresh_data
        clean=self.clean_root();output=io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0,refresh_data.main(["--root",str(clean),"start","--mode","full","--source-run","prior","--source-run-root",str(self.root),"--dry-run"]))
        result=json.loads(output.getvalue())
        self.assertTrue(result["readOnly"]);self.assertEqual(str(self.root.resolve()),result["sourceRunRoot"])
        self.assertFalse((clean/"data/refresh/runs").exists())

    def test_source_discovery_preserves_nested_evidence_and_exclusions(self):
        wanted=["data/raw/nested/evidence.json","data/raw/firm_rosters/person.csv", "data/reference/rules.json"]
        ignored=["data/raw/nested/__pycache__/cache.pyc","data/raw/CRM_Contacts_old.xlsx", "data/raw/brochure.PDF"]
        for rel in wanted+ignored:
            path=self.root/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_text("fixture")
        paths={p.relative_to(self.root).as_posix() for p in core.source_paths(self.root,"full")}
        self.assertTrue(set(wanted)<=paths);self.assertFalse(set(ignored)&paths)

if __name__=="__main__":unittest.main()
