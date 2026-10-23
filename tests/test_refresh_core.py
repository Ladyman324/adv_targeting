"""Orchestrator contracts with real children and disposable workspaces."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "src"))
import refresh_core as core
import refresh_data
import refresh_inventory
import refresh_release


class RefreshCoreTests(unittest.TestCase):
    def test_root_local_support_server_is_staged_and_integrity_pinned(self):
        source = self.write("serve.py", "# Local non-sending server contract fixture.\n")
        manifest = self.start()
        self.assertIn("serve.py", manifest["codeFiles"])
        self.assertEqual((self.stage / "serve.py").read_bytes(), source.read_bytes())
        core.check_inputs(self.stage, manifest)
        (self.stage / "serve.py").write_text("# Changed after the snapshot.\n", encoding="utf-8")
        with self.assertRaisesRegex(core.RefreshError, "Staged implementation changed"):
            core.check_inputs(self.stage, manifest)

    def test_facts_parallelizes_metadata_and_preserves_exact_file_selection(self):
        path = self.write("data/raw/example.json", "{}")
        original = Path.is_file
        caller = threading.current_thread()
        def checked(candidate):
            self.assertIsNot(threading.current_thread(), caller)
            return original(candidate)
        with mock.patch.object(Path, "is_file", checked):
            result = core.facts([path, path, self.root / "data", self.root / "missing"], self.root)
        self.assertEqual(result, {"data/raw/example.json": {"sha256": core.file_hash(path), "bytes": 2}})

    @unittest.skipUnless(shutil.which("node"), "Node required for offline transport guards")
    def test_node_transport_guard_contract(self):
        subprocess.run(["node", str(SOURCE / "tests/test_refresh_offline_node.js")], check=True)

    def test_optional_rankings_and_improved_unplaced_count_do_not_block(self):
        comparison = {"artifactAvailability": {"after": {
            "webapp/data/contacts.json": True,
            "data/interim/forbes_matches.parquet": False,
            "data/interim/barrons_rankings.parquet": False}},
            "assets": {"after": {"issues": []}},
            "coverage": {"before": {"contacts": 100, "fieldUnplaced": 100},
                         "after": {"contacts": 90, "fieldUnplaced": 0}}}
        self.assertEqual(refresh_release.check_comparison(comparison), [])
        comparison["coverage"]["after"]["contacts"] = 70
        self.assertEqual(refresh_release.check_comparison(comparison), ["contacts"])
        comparison["artifactAvailability"]["after"]["webapp/data/contacts.json"] = False
        with self.assertRaisesRegex(core.RefreshError, "contacts.json"):
            refresh_release.check_comparison(comparison)

    def test_process_identity_uses_real_os_liveness(self):
        result = core.process_identity(os.getpid())
        self.assertTrue(result["alive"])
        self.assertIsNotNone(result["created"])
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertFalse(core.process_identity(child.pid)["alive"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root, self.stages = self.base / "repo", self.base / "stages"
        self.root.mkdir()
        self.stages.mkdir()
        self.write("data/raw/act_contacts_fixture.json", "[]")
        self.write("data/raw/act_pull_manifest_fixture.json", json.dumps({"policies": {"pull_id": "fixture"}}))
        self.write("webapp/data/baseline.json", '{"untouched":true}')
        self.write("src/job.py", "from pathlib import Path\np=Path('data/output/result.txt')\np.parent.mkdir(parents=True,exist_ok=True)\np.write_text('result')\nwith Path('executions.log').open('a') as f:f.write('run\\n')\n")

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    @contextlib.contextmanager
    def sources(self):
        with mock.patch.object(refresh_inventory, "inventory", return_value=[]), mock.patch.object(refresh_inventory, "validate_act_snapshot", return_value={}):
            yield

    def start(self):
        with self.sources(), mock.patch.object(core.subprocess, "check_output", side_effect=["a" * 40, ""]):
            manifest = core.create_run(self.root, "act-only", stage_parent=self.stages, run_id="fixture")
        self.stage = Path(manifest["workspaceRoot"])
        self.directory = self.root / "data/refresh/runs/fixture"
        return manifest

    def build(self, steps=None):
        steps = steps or [core.step("job", ["src/job.py"], outputs=["data/output/result.txt"])]
        with self.sources(), mock.patch.object(core, "build_steps", return_value=steps):
            return core.build(self.root, "fixture")

    def test_five_graphs_fresh_asset_index_and_real_output_names(self):
        for mode in core.MODES:
            plan = core.build_steps(mode, "act.json", ["barrons", "forbes"])
            position = {item["id"]: i for i, item in enumerate(plan)}
            for item in plan:
                self.assertTrue(all(position[d] < position[item["id"]] for d in item["dependencies"]))
            if mode == "rankings-only":
                self.assertNotIn("crosswalk", position)
                self.assertNotIn("geography", position)
                self.assertNotIn("recipients", position)
                self.assertNotIn("desktop_search", position)
                self.assertLess(position["export_barrons"], position["match_forbes"])
                self.assertLess(position["export_forbes"], position["field_tiles"])
                continue
            self.assertLess(position["display_names"], position["assets"])
            self.assertLess(position["display_names"], position["export_forbes"])
            self.assertLess(position["export_barrons"], position["match_forbes"])
            self.assertEqual("sec_individuals" in position, mode in {"sec-only", "full"})
            by_id = {item["id"]: item for item in plan}
            self.assertIn("data/interim/advisor_placement.parquet", by_id["placement"]["outputs"])
            self.assertIn("data/interim/contact_work_branches.parquet", by_id["working_locations"]["outputs"])

    def test_targeted_mode_pins_normalized_evidence_without_unused_raw_caches(self):
        self.write("data/firm_api/one.json", "{}")
        self.write("data/firm_owners/one.json", "{}")
        self.write("data/interim/advisors.parquet", "cached SEC evidence")
        self.write("data/interim/contact_work_geocodes.csv", "cached Census evidence")
        self.write("data/interim/contact_work_google_cache.jsonl", "cached Google evidence")
        self.write("webapp/data/geo_index.json", "{}")
        selected = {p.relative_to(self.root).as_posix() for p in core.source_paths(self.root, "act-only")}
        self.assertIn("data/interim/advisors.parquet", selected)
        self.assertIn("data/interim/contact_work_geocodes.csv", selected)
        self.assertIn("data/interim/contact_work_google_cache.jsonl", selected)
        self.assertIn("webapp/data/geo_index.json", selected)
        self.assertNotIn("data/firm_api/one.json", selected)
        self.assertNotIn("data/firm_owners/one.json", selected)
        full = {p.relative_to(self.root).as_posix() for p in core.source_paths(self.root, "full")}
        self.assertIn("data/firm_api/one.json", full)
        self.start()
        (self.stage / "data/interim/advisors.parquet").write_text("changed")
        with self.assertRaisesRegex(core.RefreshError, "Pinned source inputs changed"):
            core.check_inputs(self.stage, core.read_json(self.directory / "run.json"))

    def test_private_gzip_and_correction_csv_seeded_for_targeted_reuse(self):
        paths = ["data/identity/approved_recipients.json.gz", "data/output/advisor_emails.json.gz",
                 "data/identity/approved_recipients_shards/01.json.gz", "data/identity/act_crd_corrections.csv"]
        for relative in paths:
            self.write(relative, "fixture")
        selected = {p.relative_to(self.root).as_posix() for p in core.baseline_paths(self.root)}
        self.assertTrue(set(paths).issubset(selected))

    def test_generated_list_import_stamp_is_not_an_implementation_change(self):
        path = self.write("webapp/index.html", '<script src="list_import.js?v=old"></script>')
        before = core.normalized_code_hash(path, "webapp/index.html")
        path.write_text('<script src="list_import.js?v=new"></script>')
        self.assertEqual(before, core.normalized_code_hash(path, "webapp/index.html"))
        path.write_text('<script src="other.js?v=new"></script>')
        self.assertNotEqual(before, core.normalized_code_hash(path, "webapp/index.html"))

    def test_rankings_reuse_excludes_operational_and_future_provider_outputs(self):
        self.write("webapp/data/.gzip_manifest.json", "{}")
        self.write("webapp/data/forbes.json", "{}")
        self.write("data/interim/forbes_matches.parquet", "legacy match")
        with self.sources(), mock.patch.object(core.subprocess, "check_output", side_effect=["a" * 40, ""]):
            manifest = core.create_run(self.root, "rankings-only", stage_parent=self.stages, run_id="rankfixture")
        reused = manifest["reusedArtifacts"]
        self.assertIn("webapp/data/baseline.json", reused)
        self.assertNotIn("webapp/data/.gzip_manifest.json", reused)
        self.assertNotIn("webapp/data/forbes.json", reused)
        self.assertNotIn("data/interim/forbes_matches.parquet", reused)
        stage = Path(manifest["workspaceRoot"])
        (stage / "webapp/data/.gzip_manifest.json").write_text('{"generated":true}')
        core.check_inputs(stage, manifest)
        (stage / "webapp/data/baseline.json").write_text('{"tampered":true}')
        with self.assertRaisesRegex(core.RefreshError, "pinned reused artifact changed"):
            core.check_inputs(stage, manifest)

    def test_dry_run_nonmutation(self):
        before = core.facts(list(self.root.rglob("*")), self.root)
        with self.sources(), contextlib.redirect_stdout(io.StringIO()) as capture:
            self.assertEqual(refresh_data.main(["--root", str(self.root), "start", "--mode", "act-only", "--dry-run"]), 0)
        self.assertTrue(json.loads(capture.getvalue())["readOnly"])
        self.assertEqual(core.facts(list(self.root.rglob("*")), self.root), before)
        self.assertFalse((self.root / "data/refresh").exists())

    def test_cli_start_records_bootstrap_before_single_inventory(self):
        def inspect_inventory(root):
            bootstrap = core.read_json(root / "data/refresh/runs/clistart/run.json")
            self.assertEqual(bootstrap["phase"], "capture")
            self.assertFalse(bootstrap["snapshotComplete"])
            return []
        with mock.patch.object(refresh_inventory, "inventory", side_effect=inspect_inventory) as inventory_call, mock.patch.object(refresh_inventory, "validate_act_snapshot", return_value={}), mock.patch.object(core.subprocess, "check_output", side_effect=["a" * 40, ""]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(refresh_data.main(["--root", str(self.root), "start", "--mode", "act-only", "--stage-parent", str(self.stages), "--run-id", "clistart"]), 0)
        self.assertEqual(inventory_call.call_count, 1)

    def test_cli_reads_and_resumes_bootstrap_before_marker_creation(self):
        directory = self.root / "data/refresh/runs/early"
        core.atomic_json(directory / "run.json", {"schemaVersion": 1, "id": "early", "ownerToken": "owned",
            "workspaceRoot": str(self.stages / "not-created"), "baselineRoot": str(self.root),
            "mode": "act-only", "status": "staging", "phase": "capture"})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(refresh_data.main(["--root", str(self.root), "status", "--run", "early"]), 0)
        with self.assertRaisesRegex(core.RefreshError, "Staging is incomplete"):
            core.build(self.root, "early")
        with mock.patch("refresh_stage.resume_stage") as recover, mock.patch.object(refresh_data, "build", return_value={"id": "early", "status": "built"}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(refresh_data.main(["--root", str(self.root), "resume", "--run", "early"]), 0)
        recover.assert_called_once_with(self.root.resolve(), "early")

    def test_publication_receipt_cli_is_explicit_and_dry_run_does_not_write(self):
        self.start()
        args = ["--root", str(self.root), "prepare-publication-receipt", "--run", "fixture",
                "--published-utc", "2026-10-22T12:00:00Z", "--api-commit", "a" * 40,
                "--static-fingerprint", "observed", "--recipient-registry-hash", "b" * 64,
                "--confirm-runtime-verified"]
        before = core.facts(self.root.rglob("*"), self.root)
        with mock.patch("refresh_publication.prepare_receipt", return_value={"settingChanged": False}) as prepare, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(refresh_data.main(args + ["--dry-run"]), 0)
            prepare.assert_not_called()
            self.assertEqual(before, core.facts(self.root.rglob("*"), self.root))
            self.assertEqual(refresh_data.main(args), 0)
            prepare.assert_called_once_with(self.root.resolve(), "fixture", published_utc="2026-10-22T12:00:00Z",
                api_commit="a" * 40, static_fingerprint="observed", recipient_registry_hash="b" * 64,
                confirm_runtime_verified=True)

    def test_staging_and_live_source_immutability(self):
        original = core.file_hash(self.root / "data/raw/act_contacts_fixture.json")
        self.assertEqual(self.start()["status"], "ready")
        self.build()
        self.assertNotEqual(self.stage, self.root)
        self.assertEqual(core.file_hash(self.root / "data/raw/act_contacts_fixture.json"), original)
        self.assertFalse((self.root / "data/output/result.txt").exists())
        self.assertEqual(core.read_json(self.root / "webapp/data/baseline.json"), {"untouched": True})

    def test_process_lock_contention_and_release(self):
        run = self.base / "locks"
        code = ("import sys;sys.path.insert(0,sys.argv[1]);from refresh_core import lock,RefreshError\n"
                "try:\n with lock(sys.argv[2]):pass\nexcept RefreshError:sys.exit(42)\n")
        command = [sys.executable, "-c", code, str(SOURCE / "src"), str(run)]
        with core.lock(run):
            result = subprocess.run(command, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(subprocess.run(command, capture_output=True, timeout=15).returncode, 0)

    def test_failure_propagation_and_no_dependents(self):
        self.write("src/fail.py", "raise RuntimeError('fixture crash')\n")
        self.start()
        steps = [core.step("bad", ["src/fail.py"], outputs=["missing"]), core.step("later", ["src/job.py"], ["bad"], ["data/output/result.txt"])]
        with self.assertRaisesRegex(core.RefreshError, "exit"):
            self.build(steps)
        result = core.read_json(self.directory / "run.json")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failedStep"], "bad")
        self.assertNotIn("later", result["steps"])
        self.assertIn("fixture crash", (self.directory / "logs/bad.log").read_text())

    def test_final_verification_failure_is_not_left_running(self):
        self.start()
        with mock.patch.object(core, "check_inputs", side_effect=[None, core.RefreshError("changed during build")]):
            with self.assertRaisesRegex(core.RefreshError, "changed during build"):
                self.build()
        manifest = core.read_json(self.directory / "run.json")
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["outcome"], "failed")
        self.assertEqual(manifest["errorCode"], "FINAL_VERIFICATION_FAILED")

    def test_every_required_output(self):
        self.start()
        with self.assertRaisesRegex(core.RefreshError, "missing required outputs"):
            self.build([core.step("partial", ["src/job.py"], outputs=["data/output/result.txt", "data/output/second.txt"])])

    def test_successful_noop_cannot_reuse_stale_outputs(self):
        self.write("data/output/result.txt", "old result")
        self.write("src/noop.py", "print('did nothing')\n")
        self.start()
        with self.assertRaisesRegex(core.RefreshError, "did not regenerate"):
            self.build([core.step("noop", ["src/noop.py"], outputs=["data/output/result.txt"])])

    def test_live_controller_mutation_rejects_resume(self):
        self.start()
        self.build()
        self.write("src/job.py", "print('new controller implementation')\n")
        with self.assertRaisesRegex(core.RefreshError, "controller changed"):
            self.build()

    def test_resume_reuses_verified_success_rebuilds_tampered_outputs(self):
        self.start()
        first = self.build()
        self.build()
        self.assertEqual((self.stage / "executions.log").read_text().splitlines(), ["run"])
        (self.stage / "data/output/result.txt").write_text("tampered")
        second = self.build()
        self.assertEqual((self.stage / "executions.log").read_text().splitlines(), ["run", "run"])
        self.assertEqual(second["steps"]["job"]["attempt"], first["steps"]["job"]["attempt"] + 1)

    def test_pinned_source_and_code_tamper(self):
        manifest = self.start()
        raw = self.stage / "data/raw/act_contacts_fixture.json"
        raw.write_text('[{"changed":true}]')
        with self.assertRaisesRegex(core.RefreshError, "Pinned source inputs changed"):
            core.check_inputs(self.stage, manifest)
        raw.write_text("[]")
        (self.stage / "src/job.py").write_text("raise Exception('changed')")
        with self.assertRaisesRegex(core.RefreshError, "implementation changed"):
            core.check_inputs(self.stage, manifest)

    def test_workspace_marker_and_path_traversal(self):
        self.start()
        core.atomic_json(self.stage / ".refresh-workspace.json", {"runId": "fixture", "ownerToken": "foreign"})
        with self.assertRaisesRegex(core.RefreshError, "ownership mismatch"):
            core.load_run(self.root, "fixture")
        with self.assertRaises(core.RefreshError):
            core.run_path(self.root, "../escape")
        with self.assertRaises(core.RefreshError):
            core.contained(self.root, "../escape")

    def test_swallowed_external_network_attempt_is_failed(self):
        guard = self.root / "src/refresh_offline/sitecustomize.py"
        guard.parent.mkdir(parents=True)
        shutil.copy2(SOURCE / "src/refresh_offline/sitecustomize.py", guard)
        self.write("src/network.py", "import socket\ntry:socket.getaddrinfo('example.invalid',443)\nexcept OSError:pass\nprint('swallowed')\n")
        log = self.base / "network.log"
        with self.assertRaisesRegex(core.RefreshError, "offline build attempted network"):
            core.execute_command(self.root, self.root, ["src/network.py"], log)
        self.assertIn("swallowed", log.read_text())
        self.assertIn("external_network_denied", Path(str(log) + ".network-denied").read_text())

    def test_offline_child_removes_secrets_and_external_adv(self):
        with mock.patch.dict(os.environ, {"AZURE_TOKEN": "never-log", "ACT_PASSWORD": "never-log", "AzureWebJobsStorage": "never-log", "GRAPH_CLIENT_ID": "sensitive", "ADV_SOURCE": "C:/outside.csv"}):
            env = core.child_environment(self.root, self.root, self.base / "marker")
        self.assertNotIn("AZURE_TOKEN", env)
        self.assertNotIn("ACT_PASSWORD", env)
        self.assertNotIn("AzureWebJobsStorage", env)
        self.assertNotIn("GRAPH_CLIENT_ID", env)
        self.assertNotIn("ADV_SOURCE", env)
        self.assertEqual(env["ADVISOR_REFRESH_OFFLINE"], "1")

    @unittest.skipUnless(shutil.which("node"), "Node required for offline JS child guard")
    def test_node_guard_copied_and_caught_dns_denial_fails_step(self):
        guard = self.root / "src/refresh_offline/node.cjs"
        guard.parent.mkdir(parents=True)
        shutil.copy2(SOURCE / "src/refresh_offline/node.cjs", guard)
        self.write("src/network.js", "try{require('node:dns').lookup('example.invalid',()=>{});}catch{};console.log('caught denial');\n")
        self.start()
        self.assertTrue((self.stage / "src/refresh_offline/node.cjs").is_file())
        log = self.directory / "node-guard.log"
        with self.assertRaisesRegex(core.RefreshError, "offline build attempted network"):
            core.execute_command(self.stage, self.root, ["node", "src/network.js"], log)
        self.assertIn("caught denial", log.read_text())

    def test_release_tamper_and_dirty_production_guard(self):
        manifest = self.start()
        from test_refresh_release import private_fixture
        private = private_fixture(self.stage)
        artifact = self.stage / "webapp/data/baseline.json"
        manifest["status"] = "candidate_ready"
        manifest["validation"] = {"status": "success", "runTests": True,
            "artifacts": core.facts(core.paths_matching(self.stage, refresh_release.ARTIFACT_PATTERNS), self.stage)}
        core.save_run(self.directory, manifest)
        package = self.directory / "candidate/static.tgz"
        package.parent.mkdir()
        package.write_bytes(b"fixture archive bytes")
        for relative, source in private.items():
            target = package.parent / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        candidate = {"runId": "fixture", "codeHash": manifest["codeHash"], "validationHash": core.digest(manifest["validation"]), "dirty": True,
            "files": core.facts([p for p in package.parent.rglob("*") if p.is_file()], package.parent)}
        candidate["contentHash"] = core.digest(candidate)
        core.atomic_json(package.parent / "release.json", candidate)
        manifest["candidate"] = {"contentHash": candidate["contentHash"]}
        core.save_run(self.directory, manifest)
        self.assertTrue(refresh_release.verify_release(self.root, "fixture")["verified"])
        with self.assertRaisesRegex(core.RefreshError, "Production requires committed"):
            refresh_release.verify_release(self.root, "fixture", production=True)
        package.write_bytes(b"tampered package")
        with self.assertRaisesRegex(core.RefreshError, "package or dependency changed"):
            refresh_release.verify_release(self.root, "fixture")
        package.write_bytes(b"fixture archive bytes")
        artifact.write_text('{"tampered":true}')
        with self.assertRaisesRegex(core.RefreshError, "artifacts changed"):
            refresh_release.verify_release(self.root, "fixture")


if __name__ == "__main__":
    unittest.main()
