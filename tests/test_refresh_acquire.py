"""Source adapter fixture tests: no real acquisition or production writes."""
import csv
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import refresh_core as core
import refresh_acquire as acquire
from contact_provenance import build_manifest, file_facts


def pair(raw, stamp="2026-09-25T022553Z"):
    raw.mkdir(parents=True, exist_ok=True)
    contacts = raw / ("act_contacts_" + stamp + ".json")
    owners = raw / ("act_eic_contact_" + stamp + ".json")
    core.atomic_json(contacts, [{"id": "act-1"}, {"id": "act-2"}])
    core.atomic_json(owners, {"schema_version": 1, "pull_id": stamp, "contacts_file": contacts.name,
        "contacts_sha256": core.file_hash(contacts), "contacts_rows": 2, "complete": True,
        "failed_codes": [], "expected_codes": ["SH"], "queried_codes": ["SH"],
        "counts": {"SH": 1}, "conflicts": [], "owner_by_contact_id": {"act-1": "SH"}})
    manifest = raw / ("act_pull_manifest_" + stamp + ".json")
    core.atomic_json(manifest, build_manifest([file_facts(contacts, "act_contacts", rows=2),
        file_facts(owners, "act_eic_contact", rows=1)], policies={"pull_id": stamp,
        "pair_complete": True, "partial_refreshes": "forbidden"}))
    return manifest


def roster(path, rows=10, fields=("name", "email", "phone", "city", "state")):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
        for n in range(rows):
            values = {"name": "Person " + str(n), "email": "person" + str(n) + "@example.com",
                      "phone": "4045551234", "city": "Atlanta", "state": "GA"}
            writer.writerow({k: values.get(k, "Value") for k in fields})
    return path


def sec_firms_xml(count=1):
    return ("<Root>" + "".join('<Firm><Info FirmCrdNb="' + str(i + 1)
        + '" BusNm="Fixture firm ' + str(i + 1) + '" LegalNm="Fixture legal name"/>'
        '<MainAddr City="Atlanta" State="GA" Strt1="Fixture street"/></Firm>'
        for i in range(count)) + "</Root>").encode("utf-8")


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"; self.stage = Path(self.temp.name) / "stage"
        self.root.mkdir(); self.stage.mkdir()
        pair(self.stage / "data/raw")
        self.directory = core.run_path(self.root, "fixture"); self.directory.mkdir(parents=True)
        core.atomic_json(self.stage / ".refresh-workspace.json", {"runId": "fixture", "ownerToken": "token"})
        self.value = {"schemaVersion": 1, "id": "fixture", "ownerToken": "token", "workspaceRoot": str(self.stage),
            "baselineRoot": str(self.root),
            "mode": "full", "firms": [],
            "codeFiles": {}, "codeNormalization": {}, "inputs": core.facts(core.source_paths(self.stage), self.stage),
            "imports": [], "attempts": [], "steps": {}, "warnings": [], "status": "ready"}
        core.save_run(self.directory, self.value)
        self.incoming = Path(self.temp.name) / "incoming"; self.incoming.mkdir()

    def pin(self):
        _, manifest, _ = core.load_run(self.root, "fixture")
        manifest["inputs"] = core.facts(core.source_paths(self.stage), self.stage)
        core.save_run(self.directory, manifest)

    def approve_held(self, sid):
        from refresh_review import review
        from refresh_history import source_key
        reports = list((self.stage / "data/refresh/acquisition_reviews" / source_key(sid)).glob("*/comparison.json"))
        self.assertEqual(len(reports), 1)
        report = core.read_json(reports[0])
        review(self.root, "fixture", reports[0].parent.name, "approve", "Fixture operator",
               "Reviewed genuine supplier coverage change", sorted({row["code"] for row in report["issues"]}),
               source_id=sid)

    def test_roster_import_and_immutable_name(self):
        src = roster(self.incoming / "raymond_james_2026-10-22.csv")
        result = acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", src)
        selected = self.stage / "data/raw/firm_rosters" / src.name
        self.assertEqual(core.file_hash(src), core.file_hash(selected))
        self.assertEqual(result["imports"][-1]["records"], 10)
        self.assertFalse((self.root / "data/raw").exists())
        roster(src, rows=11)
        with self.assertRaisesRegex(core.RefreshError, "Immutable"):
            acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", src)
        with selected.open() as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 10)
        self.assertEqual(core.read_json(self.directory / "run.json")["attempts"][-1]["status"], "failed")

    def test_interrupted_copy_never_leaves_final_partial_source(self):
        source = roster(self.incoming / "raymond_james_2026-10-22.csv")
        target = self.stage / "data/raw/firm_rosters" / source.name
        def partial(src, dst):
            Path(dst).write_bytes(b"partial download")
            raise OSError("Synthetic interrupted local copy")
        with patch.object(acquire.shutil, "copy2", side_effect=partial):
            with self.assertRaises(OSError): acquire._copy(source, target)
        self.assertFalse(target.exists())
        self.assertFalse(list(target.parent.glob(".refresh-import-*")))

    def test_corrupted_copy_is_not_selected_and_last_good_is_restored(self):
        existing = roster(self.stage / "data/raw/firm_rosters/raymond_james_2026-10-01.csv")
        before = existing.read_bytes(); self.pin()
        source = roster(self.incoming / "raymond_james_2026-10-22.csv")
        real_copy = acquire.shutil.copy2
        def copy(src, dst):
            if Path(src).name == source.name:
                Path(dst).write_bytes(b"silently corrupted copy")
                return str(dst)
            return real_copy(src, dst)
        with patch.object(acquire.shutil, "copy2", side_effect=copy):
            with self.assertRaisesRegex(core.RefreshError, "copy verification failed"):
                acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", source)
        self.assertEqual(existing.read_bytes(), before)
        self.assertFalse((existing.parent / source.name).exists())
        self.assertFalse(list(existing.parent.glob(".refresh-import-*")))

    def test_reduction_requires_recorded_review(self):
        roster(self.stage / "data/raw/firm_rosters/raymond_james_2026-10-01.csv", rows=100); self.pin()
        src = roster(self.incoming / "raymond_james_2026-10-22.csv", rows=79)
        with self.assertRaisesRegex(core.RefreshError, "SOURCE_ENTITIES_DISAPPEARED"):
            acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", src)
        with self.assertRaisesRegex(core.RefreshError, "held candidate"):
            acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", src,
                                    reduction_reason="Reviewed genuine departures")
        self.approve_held("roster:raymond_james")
        result = acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", src, reduction_reason="Reviewed genuine departures")
        self.assertEqual(result["imports"][-1]["reductionReason"], "Reviewed genuine departures")
        self.assertEqual(len(list((self.stage / "data/raw/firm_rosters").glob("raymond_james_*.csv"))), 1)

    def test_empty_bad_schema_and_blank_contact_rejected(self):
        src = roster(self.incoming / "raymond_james_2026-10-22.csv", fields=("name",))
        with self.assertRaises(core.RefreshError):
            acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", src)
        roster(src, rows=0)
        with self.assertRaises(core.RefreshError):
            acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", src)

    def test_same_size_contact_loss_is_blocked_but_reviewable(self):
        roster(self.stage / "data/raw/firm_rosters/raymond_james_2026-10-01.csv", rows=100); self.pin()
        src = roster(self.incoming / "raymond_james_2026-10-22.csv", rows=100)
        with src.open(newline="") as handle:
            reader = csv.DictReader(handle); fields, rows = reader.fieldnames, list(reader)
        for row in rows[1:]:
            row["email"], row["phone"] = "", ""
        with src.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
        with self.assertRaisesRegex(core.RefreshError, "FIELD_COVERAGE_LOST"):
            acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", src)
        with self.assertRaisesRegex(core.RefreshError, "12 characters"):
            acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", src, reduction_reason="reviewed")
        self.approve_held("roster:raymond_james")
        result = acquire.import_snapshot(self.root, "fixture", "roster:raymond_james", src,
                                        reduction_reason="Reviewed supplier channel removals")
        event = result["imports"][-1]
        self.assertEqual(event["records"], 100)
        self.assertEqual(event["emailRecords"], 1)
        self.assertEqual(event["phoneRecords"], 1)
        self.assertEqual(event["priorCoverage"]["emailRecords"], 100)
        self.assertIn("contactRecords", event["coverageLosses"])

    def test_phone_only_roster_does_not_require_an_unavailable_email_channel(self):
        baseline = roster(self.incoming / "old.csv", rows=100,
                          fields=("name", "office_phone", "office_city", "office_state"))
        current = roster(self.incoming / "new.csv", rows=100,
                         fields=("name", "office_phone", "office_city", "office_state"))
        result = acquire._roster(current, "captrust", baseline)
        self.assertEqual(result["emailRecords"], 0)
        self.assertEqual(result["phoneRecords"], 100)
        self.assertEqual(result["coverageLosses"], [])

    def test_morgan_directory_only_held_then_bound_completeness_accepted(self):
        src = roster(self.incoming / "morgan_stanley_2026-10-22.csv",
                     fields=("Name", "Email", "Main Phone", "City", "State", "Entity Type"))
        with self.assertRaisesRegex(core.RefreshError, "completeness"):
            acquire.import_snapshot(self.root, "fixture", "roster:morgan_stanley", src)
        core.atomic_json(src.with_suffix(".completeness.json"), {"complete": True,
            "teamComplete": True, "officeComplete": True, "sha256": core.file_hash(src)})
        result = acquire.import_snapshot(self.root, "fixture", "roster:morgan_stanley", src)
        self.assertEqual(result["imports"][-1]["completeness"], "verified")

    def test_ranking_import_preserves_baseline_and_freezes_edition(self):
        baseline = self.stage / "data/raw/forbes_rankings.json"
        core.atomic_json(baseline, [{"name": "Old Person", "list": "top-advisors", "category": "Georgia", "year": 2024}]); before = baseline.read_bytes(); self.pin()
        src = self.incoming / "forbes_rankings.json"
        core.atomic_json(src, [{"name": "New Person", "list": "top-advisors", "category": "Georgia"},
            {"name": "Other Person", "list": "not-selected", "category": "Georgia"}])
        result = acquire.import_snapshot(self.root, "fixture", "forbes", src, edition="2026", categories=("top-advisors",))
        self.assertEqual(baseline.read_bytes(), before)
        registered = self.stage / result["rankingImports"][-1]["path"]
        self.assertEqual(core.read_json(registered)[0]["year"], 2026)
        self.assertEqual(len(core.read_json(registered)), 1)
        self.assertTrue(list((self.stage / "data/refresh/source_archive/forbes").glob("original-*/forbes_rankings.json")))
        self.assertEqual(registered.parent, self.stage / "data/refresh/ranking_editions/forbes")
        with self.assertRaisesRegex(core.RefreshError, "category"):
            acquire.import_snapshot(self.root, "fixture", "forbes", src, edition="2026", categories=("missing",))

    def test_older_sec_selection_quarantines_newer_only_in_stage(self):
        raw = self.stage / "data/raw"; newer = raw / "IA_FIRM_SEC_Feed_10_22_2026.xml.gz"
        with gzip.open(newer, "wb") as handle: handle.write(sec_firms_xml())
        self.pin()
        src = self.incoming / "IA_FIRM_SEC_Feed_09_01_2026.xml.gz"
        with gzip.open(src, "wb") as handle: handle.write(sec_firms_xml(2))
        result = acquire.import_snapshot(self.root, "fixture", "sec_firms", src)
        self.assertEqual([p.name for p in raw.glob("IA_FIRM_SEC*")], [src.name])
        self.assertTrue(list((self.stage / "data/refresh/quarantine").rglob(newer.name)))
        self.assertEqual(result["imports"][-1]["records"], 2)
        self.assertEqual(result["imports"][-1]["sourceDate"], "2026-09-01")

    def test_invalid_sec_and_adv_rejected(self):
        src = self.incoming / "IA_INDVL_Feed_10_22_2026.xml.zip"
        with zipfile.ZipFile(src, "w") as archive: archive.writestr("feed.xml", "<Root><Firm/></Root>")
        with self.assertRaisesRegex(core.RefreshError, "SEC_FORMAT_INVALID"):
            acquire.import_snapshot(self.root, "fixture", "sec_individuals", src)
        adv = self.incoming / "ia102226.zip"
        with zipfile.ZipFile(adv, "w") as archive: archive.writestr("firms.csv", "name\nbad")
        with self.assertRaisesRegex(core.RefreshError, "ADV"):
            acquire.import_snapshot(self.root, "fixture", "sec_adv", adv)

    def test_adv_requires_actual_downstream_schema_and_explicit_selection(self):
        from adv import COLS
        src = self.incoming / "ia102226.csv"
        with src.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(COLS.values()))
            writer.writeheader(); writer.writerow({key: "1" for key in COLS.values()})
        result = acquire.import_snapshot(self.root, "fixture", "sec_adv", src, source_date="2026-10-22")
        self.assertEqual(result["imports"][-1]["records"], 1)
        self.assertEqual(len(list((self.stage / "data/raw").glob("refresh_adv_*"))), 1)
        with src.open("a") as handle: handle.write("\n")
        src = src.rename(self.incoming / "ia102326.csv")
        acquire.import_snapshot(self.root, "fixture", "sec_adv", src, source_date="2026-10-22")
        self.assertEqual(len(list((self.stage / "data/raw").glob("refresh_adv_*"))), 1)

    def test_act_pair_validated_and_mismatch_retains_prior(self):
        src = pair(self.incoming, "2026-10-22T120000Z")
        result = acquire.import_snapshot(self.root, "fixture", "act", src)
        self.assertEqual(result["imports"][-1]["records"], 2)
        bad = pair(self.incoming, "2026-10-23T120000Z")
        core.atomic_json(self.incoming / "act_contacts_2026-10-23T120000Z.json", [{"id": "tampered"}])
        with self.assertRaisesRegex(core.RefreshError, "provenance"):
            acquire.import_snapshot(self.root, "fixture", "act", bad)
        self.assertTrue((self.stage / "data/raw" / src.name).exists())
        self.assertFalse((self.stage / "data/raw" / bad.name).exists())

    def test_acquisition_network_gate_and_handoffs_do_not_run_commands(self):
        with patch.object(core, "execute_command") as execute:
            with self.assertRaisesRegex(core.RefreshError, "allow-network"):
                acquire.acquire(self.root, "fixture", "sec_firms")
            for sid in ("barrons", "forbes", "sec_adv"):
                with self.assertRaises(core.RefreshError):
                    acquire.acquire(self.root, "fixture", sid, allow_network=True)
            execute.assert_not_called()
        receipt = core.read_json(self.stage / "data/refresh/source_receipts.json")
        self.assertEqual(receipt["sec_firms"]["attemptStatus"], "failed")

    def test_failed_acquisition_preserves_source_and_retrieval_receipt(self):
        receipt = self.stage / "data/refresh/source_receipts.json"
        core.atomic_json(receipt, {"sec_firms": {"retrievedUtc": "2026-09-01T12:00:00Z", "lastSuccessUtc": "2026-09-01T12:00:00Z"}}); self.pin()
        with self.assertRaises(core.RefreshError):
            acquire.acquire(self.root, "fixture", "sec_firms")
        row = core.read_json(receipt)["sec_firms"]
        self.assertEqual(row["retrievedUtc"], "2026-09-01T12:00:00Z")
        self.assertEqual(row["lastSuccessUtc"], "2026-09-01T12:00:00Z")
        self.assertEqual(row["status"], "failed")

    def test_existing_commands_keep_morgan_three_stages_and_edj_browser_safe(self):
        commands = acquire.acquisition_commands("roster:morgan_stanley")
        self.assertEqual([c[0] for c in commands], ["src/morgan_stanley_async.py", "src/morgan_office_async.py", "src/morgan_team_async.py"])
        self.assertIn("--no-launch-chrome", acquire.acquisition_commands("roster:edward_jones")[0])
        with self.assertRaises(core.RefreshError):
            acquire.acquisition_commands("act")
        self.assertNotIn("password", " ".join(acquire.acquisition_commands("act", "user", "db")[0]).lower())

    def test_mode_and_selected_firm_boundaries_before_any_side_effects(self):
        src = roster(self.incoming / "raymond_james_2026-10-22.csv")
        for mode, firms, sid in [
            ("act-only", [], "sec_firms"), ("sec-only", [], "act"),
            ("rankings-only", [], "roster:raymond_james"),
            ("selected-firm", ["ubs"], "roster:raymond_james"),
            ("selected-firm", ["ubs"], "sec_firms"),
        ]:
            _, manifest, _ = core.load_run(self.root, "fixture")
            manifest.update(mode=mode, firms=firms)
            core.save_run(self.directory, manifest)
            before = (self.directory / "run.json").read_bytes()
            with self.assertRaisesRegex(core.RefreshError, "outside"):
                acquire.import_snapshot(self.root, "fixture", sid, src)
            with self.assertRaisesRegex(core.RefreshError, "outside"):
                acquire.acquire(self.root, "fixture", sid, allow_network=True)
            self.assertEqual((self.directory / "run.json").read_bytes(), before)
            self.assertFalse((self.stage / "data/refresh/source_receipts.json").exists())
        for mode, firms, sid in [
            ("act-only", [], "act"), ("sec-only", [], "sec_adv"),
            ("rankings-only", [], "forbes"), ("selected-firm", ["ubs"], "roster:ubs"),
            ("full", [], "roster:raymond_james"),
        ]:
            acquire._compatible({"mode": mode, "firms": firms}, sid)

    def mock_script(self):
        script = self.stage / "src/fetch_feeds.py"
        script.parent.mkdir(parents=True, exist_ok=True); script.write_text("# fixture only\n")
        controller = self.root / "src/fetch_feeds.py"
        controller.parent.mkdir(parents=True, exist_ok=True); controller.write_bytes(script.read_bytes())
        _, value, _ = core.load_run(self.root, "fixture")
        value["codeFiles"] = core.facts([script], self.stage)
        value["codeNormalization"] = {"src/fetch_feeds.py": core.normalized_code_hash(script, "src/fetch_feeds.py")}
        core.save_run(self.directory, value)

    def test_acquire_uses_private_scratch_then_registers_validated_bytes(self):
        self.mock_script()
        def execute(work, root, command, log, online=False):
            self.assertTrue(online); self.assertNotEqual(work, self.stage)
            self.assertEqual(command, ["src/fetch_feeds.py", "firm_sec"])
            target = work / "data/raw/IA_FIRM_SEC_Feed_10_22_2026.xml.gz"
            with gzip.open(target, "wb") as handle: handle.write(sec_firms_xml())
        with patch.object(core, "execute_command", side_effect=execute):
            result = acquire.acquire(self.root, "fixture", "sec_firms", allow_network=True)
        self.assertEqual(result["attempts"][-1]["status"], "success")
        self.assertTrue(result["imports"][-1]["retrievedUtc"])
        self.assertTrue((self.stage / "data/raw/IA_FIRM_SEC_Feed_10_22_2026.xml.gz").exists())
        self.assertFalse((self.root / "data/raw").exists())
        core.check_inputs(self.stage, result)

    def test_partial_acquisition_does_not_replace_last_good(self):
        self.mock_script()
        existing = self.stage / "data/raw/IA_FIRM_SEC_Feed_09_01_2026.xml.gz"
        with gzip.open(existing, "wb") as handle: handle.write(sec_firms_xml())
        before = existing.read_bytes(); self.pin()
        def execute(work, root, command, log, online=False):
            (work / "data/raw/IA_FIRM_SEC_Feed_10_22_2026.xml.gz").write_bytes(b"truncated")
        with patch.object(core, "execute_command", side_effect=execute):
            with self.assertRaisesRegex(core.RefreshError, "SEC_FORMAT_INVALID"):
                acquire.acquire(self.root, "fixture", "sec_firms", allow_network=True)
        self.assertEqual(existing.read_bytes(), before)
        self.assertFalse((self.stage / "data/raw/IA_FIRM_SEC_Feed_10_22_2026.xml.gz").exists())

    def test_command_failure_records_safe_incident_and_keeps_retrieval_unknown(self):
        self.mock_script()
        existing = self.stage / "data/raw/IA_FIRM_SEC_Feed_09_01_2026.xml.gz"
        with gzip.open(existing, "wb") as handle: handle.write(sec_firms_xml())
        before = existing.read_bytes(); self.pin()
        for failure in (RuntimeError("SECRET=token private@example.com"), KeyboardInterrupt()):
            with patch.object(core, "execute_command", side_effect=failure):
                with self.assertRaises(type(failure)):
                    acquire.acquire(self.root, "fixture", "sec_firms", allow_network=True)
            _, value, _ = core.load_run(self.root, "fixture")
            event = value["attempts"][-1]
            self.assertIn("acquisitionIncidentId", event)
            file = self.stage / "data/refresh/acquisition_incidents" / event["acquisitionIncidentId"] / "incident.json"
            incident = core.read_json(file)
            self.assertEqual(incident["status"], "manual_review")
            self.assertFalse(incident["modelAuthorized"])
            self.assertNotIn("SECRET", file.read_text())
            self.assertNotIn("private@example.com", file.read_text())
            self.assertEqual(existing.read_bytes(), before)
            receipts = core.read_json(self.stage / "data/refresh/source_receipts.json")
            self.assertFalse(receipts["sec_firms"].get("retrievedUtc"))

    def test_unknown_import_replaces_retrieval_only_for_different_snapshot(self):
        from refresh_inventory import inventory
        self.mock_script()
        def execute(work, root, command, log, online=False):
            with gzip.open(work / 'data/raw/IA_FIRM_SEC_Feed_10_22_2026.xml.gz', 'wb') as handle:
                handle.write(sec_firms_xml())
        with patch.object(core, 'execute_command', side_effect=execute):
            first = acquire.acquire(self.root, 'fixture', 'sec_firms', allow_network=True)
        old = next(row for row in first['sources'] if row['id'] == 'sec_firms')
        self.assertTrue(old['retrievedUtc'])
        with patch.object(core, 'execute_command'):
            unchanged = acquire.acquire(self.root, 'fixture', 'sec_firms', allow_network=True)
        self.assertNotIn('retrievedUtc', unchanged['imports'][-1])
        retained = next(row for row in unchanged['sources'] if row['id'] == 'sec_firms')
        self.assertEqual(retained['retrievedUtc'], old['retrievedUtc'])
        self.assertEqual(retained['fileFacts'], old['fileFacts'])
        selected = self.stage / 'data/raw/IA_FIRM_SEC_Feed_10_22_2026.xml.gz'
        same = acquire.import_snapshot(self.root, 'fixture', 'sec_firms', selected)
        reused = next(row for row in same['sources'] if row['id'] == 'sec_firms')
        self.assertEqual(reused['retrievedUtc'], old['retrievedUtc'])
        self.assertEqual(reused['snapshotId'], old['snapshotId'])
        incoming = self.incoming / 'IA_FIRM_SEC_Feed_10_23_2026.xml.gz'
        with gzip.open(incoming, 'wb') as handle:
            handle.write(sec_firms_xml(2))
        result = acquire.import_snapshot(self.root, 'fixture', 'sec_firms', incoming)
        current = next(row for row in result['sources'] if row['id'] == 'sec_firms')
        self.assertIsNone(current['retrievedUtc'])
        self.assertIsNone(current['lastSuccessUtc'])
        self.assertNotEqual(current['snapshotId'], old['snapshotId'])
        self.assertEqual(current['sourceDate'], '2026-10-23')
        self.assertEqual(current['records'], 2)
        receipt = core.read_json(self.stage / 'data/refresh/source_receipts.json')['sec_firms']
        self.assertIsNone(receipt['retrievedUtc'])
        self.assertEqual(receipt['snapshotId'], current['snapshotId'])
        observed = next(row for row in inventory(self.stage) if row['id'] == 'sec_firms')
        self.assertEqual(observed['sha256'], current['sha256'])
        self.assertIsNone(observed['retrievedUtc'])

    def test_new_act_import_keeps_manifest_date_and_failed_attempt_keeps_last_good(self):
        from refresh_inventory import inventory
        old = next(row for row in inventory(self.stage) if row['id'] == 'act')
        core.atomic_json(self.stage / 'data/refresh/source_receipts.json', {'act': {
            'snapshotId': old['snapshotId'], 'retrievedUtc': old['retrievedUtc'],
            'lastSuccessUtc': old['lastSuccessUtc']}})
        self.pin()
        src = pair(self.incoming, '2026-10-22T120000Z')
        payload = core.read_json(src)
        payload['generated_utc'] = '2026-10-22T12:01:00Z'
        core.atomic_json(src, payload)
        result = acquire.import_snapshot(self.root, 'fixture', 'act', src)
        current = next(row for row in result['sources'] if row['id'] == 'act')
        self.assertEqual(current['retrievedUtc'], '2026-10-22T12:01:00Z')
        self.assertEqual(current['lastSuccessUtc'], current['retrievedUtc'])
        with self.assertRaises(core.RefreshError):
            acquire.acquire(self.root, 'fixture', 'act')
        failed = next(row for row in inventory(self.stage) if row['id'] == 'act')
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['attemptStatus'], 'failed')
        self.assertEqual(failed['retrievedUtc'], current['retrievedUtc'])
        self.assertEqual(failed['fileFacts'], current['fileFacts'])
        self.assertEqual(failed['records'], 2)


if __name__ == "__main__":
    unittest.main()
