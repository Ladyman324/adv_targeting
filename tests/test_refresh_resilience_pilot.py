"""Full local pilot replay: real controller/worker, mocked proposal provider.

No real source fetch, API call, application dataset, Git write, or deployment.
"""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import refresh_acquire as acquire
import refresh_core as core
import refresh_data as cli
import refresh_repair as repair
import refresh_repair_runner as runner
import refresh_repair_worker as worker
from test_refresh_acquire import pair
from test_refresh_resilience import records, write
from test_refresh_repair_runner import response
from test_refresh_repair_validation import SOURCE, FIXED_FUNCTION


class CompleteRosterPilotTests(unittest.TestCase):
    def test_hold_propose_validate_review_export_and_new_pinned_run(self):
        with tempfile.TemporaryDirectory(prefix="resilience-pilot-") as tmp:
            base = Path(tmp); root = base / "repo"; root.mkdir()
            stage_parent = base / "stages"; stage_parent.mkdir()
            adapter = root / repair.ADAPTER; adapter.parent.mkdir(parents=True)
            adapter.write_text(SOURCE, encoding="utf-8")
            original_code = adapter.read_bytes()
            pair(root / "data/raw")
            baseline = write(root / "data/raw/firm_rosters/edward_jones_2026-09-22.csv", records())
            original_roster = baseline.read_bytes()
            # Real capture and immutable copy plan, with a valid native ACT
            # pair. Missing Git metadata is truthfully recorded, not mocked.
            first = core.create_run(root, "selected-firm", ["edward_jones"], stage_parent, "pilot-before")
            stage = Path(first["workspaceRoot"])
            core.check_inputs(stage, first)
            incoming = base / "incoming/edward_jones_2026-10-23.csv"
            bad_rows = records()
            old_parser = worker.restricted_namespace(SOURCE)
            for row in bad_rows:
                page = '<input name="field_email_override_v2" value="' + row["email"] + '">'
                found, status = old_parser["extract_emails"](page)
                row["email"] = found[0] if found else ""
                row["email_status"] = status
            write(incoming, bad_rows)
            with self.assertRaisesRegex(core.RefreshError, "held candidate"):
                acquire.import_snapshot(root, "pilot-before", repair.SOURCE, incoming)
            self.assertEqual(baseline.read_bytes(), original_roster)
            self.assertEqual((stage / baseline.relative_to(root)).read_bytes(), original_roster)
            requests = list((stage / "data/refresh/repair_requests").glob("*/request.json"))
            self.assertEqual(len(requests), 1)
            request = core.read_json(requests[0]); identity = request["id"]
            folder = requests[0].parent
            failure = base / "failure.json"
            core.atomic_json(failure, {"kind": "synthetic", "cases": [{
                "html": '<input name="field_email_override_v2" value="new@example.invalid">',
                "expectedEmails": ["new@example.invalid"], "expectedBio": None}]})
            regression = base / "regression.json"
            core.atomic_json(regression, {"kind": "synthetic", "cases": [{
                "html": '<input name="field_email_override" value="separate@example.invalid">',
                "expectedEmails": ["separate@example.invalid"], "expectedBio": None}]})
            def command(*args):
                with contextlib.redirect_stdout(io.StringIO()):
                    return cli.main(["--root", str(root), *args])
            proposal_args = ("propose-repair", "--run", "pilot-before", "--request", identity,
                "--fixture", str(failure), "--model", "fixture-model", "--operator", "Pilot submitter",
                "--reason", "Authorized synthetic fixture to reproduce the format change", "--allow-ai",
                "--confirm-public-or-synthetic-fixture")
            with patch.object(runner, "request_model", side_effect=core.RefreshError("Synthetic transport failure")):
                with self.assertRaises(core.RefreshError): command(*proposal_args)
            failed_attempt = core.read_json(folder / "request.json")["attempts"][0]
            retry_args = ("reauthorize-repair", "--run", "pilot-before", "--request", identity,
                "--operator", "Pilot retry reviewer", "--reason", "Reviewed terminal failure and approved one more attempt",
                "--acknowledge-possible-charge")
            before_retry = (folder / "request.json").read_bytes()
            self.assertEqual(command(*retry_args, "--dry-run"), 0)
            self.assertEqual((folder / "request.json").read_bytes(), before_retry)
            self.assertEqual(command(*retry_args), 0)
            with patch.object(runner, "request_model", return_value=response(changes=[
                    {"function": "extract_emails", "source": FIXED_FUNCTION}])) as provider:
                self.assertEqual(command("propose-repair", "--run", "pilot-before", "--request", identity,
                    "--fixture", str(failure), "--model", "fixture-model", "--operator", "Pilot submitter",
                    "--reason", "Authorized synthetic fixture to reproduce the format change", "--allow-ai",
                    "--confirm-public-or-synthetic-fixture"), 0)
                self.assertEqual(provider.call_count, 1)
            self.assertEqual(core.read_json(folder / "request.json")["attempts"][0], failed_attempt)
            self.assertEqual(command("validate-repair", "--run", "pilot-before", "--request", identity,
                                     "--regression-fixture", str(regression)), 0)
            self.assertEqual(command("review-repair", "--run", "pilot-before", "--request", identity,
                "--decision", "approve", "--reviewer", "Independent pilot reviewer",
                "--reason", "Inspected parser-only changes and reproduced passing hidden regressions"), 0)
            self.assertEqual(command("export-repair", "--run", "pilot-before", "--request", identity), 0)
            exported = core.read_json(folder / "approved-change.json")
            self.assertTrue(exported["requiresNewRefreshRun"])
            self.assertFalse(exported["canApplyPatch"])
            self.assertEqual(adapter.read_bytes(), original_code)
            self.assertEqual((stage / repair.ADAPTER).read_bytes(), original_code)
            _, failed, _ = core.load_run(root, "pilot-before")
            self.assertEqual(failed["attempts"][-1]["status"], "failed")
            self.assertEqual(failed["imports"], [])
            self.assertNotIn("candidate", failed)
            core.check_inputs(stage, failed)
            # Simulate the separately reviewed source adoption in this
            # disposable repository only. No in-flight stage is patched.
            adapter.write_text(exported["replacement"], encoding="utf-8")
            with self.assertRaisesRegex(core.RefreshError, "controller changed"):
                core.check_inputs(stage, failed)
            second = core.create_run(root, "selected-firm", ["edward_jones"], stage_parent, "pilot-after")
            new_stage = Path(second["workspaceRoot"])
            self.assertNotEqual(first["codeHash"], second["codeHash"])
            self.assertEqual((stage / repair.ADAPTER).read_bytes(), original_code)
            self.assertEqual((new_stage / repair.ADAPTER).read_text(encoding="utf-8"), exported["replacement"])
            fixed_parser = worker.restricted_namespace(exported["replacement"])
            good_rows = records()
            for row in good_rows:
                page = '<input name="field_email_override_v2" value="' + row["email"] + '">'
                found, status = fixed_parser["extract_emails"](page)
                self.assertEqual(found, [row["email"]])
                row["email"] = found[0]; row["email_status"] = status
            write(incoming, good_rows)
            final = acquire.import_snapshot(root, "pilot-after", repair.SOURCE, incoming)
            self.assertEqual(final["status"], "ready")
            self.assertEqual(final["imports"][-1]["historicalAcceptance"]["status"], "accepted")
            self.assertEqual(final["imports"][-1]["records"], 100)
            core.check_inputs(new_stage, final)
            self.assertEqual(baseline.read_bytes(), original_roster)
            self.assertFalse((root / "webapp/data/contacts.json").exists())
            self.assertFalse((root / "dist").exists())
            self.assertFalse(core.active_children(folder))


if __name__ == "__main__":
    unittest.main()
