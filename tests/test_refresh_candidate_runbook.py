"""Exercise documented candidate transfer without contacting any cloud service."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "docs/data_refresh.md").read_text(encoding="utf-8")
SNIPPET = re.search(r"\$candidateUpload = @'\n(.*?)\n'@", TEXT, re.S).group(1)


class CandidateRunbookTests(unittest.TestCase):
    def execute_upload(self, candidate, shard_status=0):
        calls = []
        recipients = types.ModuleType("export_approved_recipients")
        emails = types.ModuleType("export_advisor_emails")
        recipients.upload_shards = lambda manifest, shards: (
            calls.append(("shards", manifest, shards)) or shard_status)
        emails.upload = lambda path: calls.append(("emails", path)) or 0
        with mock.patch.dict(sys.modules, {
            "export_approved_recipients": recipients,
            "export_advisor_emails": emails,
        }), mock.patch.object(sys, "argv", ["documented-upload", str(candidate)]), \
                mock.patch.object(sys, "path", list(sys.path)):
            with self.assertRaises(SystemExit) as exited:
                exec(compile(SNIPPET, "documented-candidate-upload", "exec"), {})
        return exited.exception.code, calls

    def fixture(self, candidate, prefix="fixture"):
        directory = candidate / "lookups/approved_recipients/releases" / prefix
        directory.mkdir(parents=True)
        blob = "approved_recipients/releases/" + prefix + "/shards/00.json.gz"
        manifest = directory / "manifest.json"
        manifest.write_text(json.dumps({"shards": {"00": {"blob": blob}}}),
                            encoding="utf-8")
        return manifest, candidate / "lookups" / blob

    def test_documented_upload_uses_exact_candidate_paths_and_stops_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp)
            manifest, shard = self.fixture(candidate)
            code, calls = self.execute_upload(candidate)
            self.assertEqual(code, 0)
            self.assertEqual(calls, [
                ("shards", manifest, [shard]),
                ("emails", candidate / "lookups/advisor_emails.json.gz"),
            ])
            code, calls = self.execute_upload(candidate, shard_status=1)
            self.assertEqual(code, 1)
            self.assertEqual(calls, [("shards", manifest, [shard])])

    def test_documented_upload_rejects_missing_or_ambiguous_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp)
            for count in (0, 2):
                if count:
                    self.fixture(candidate, "one")
                    self.fixture(candidate, "two")
                code, calls = self.execute_upload(candidate)
                self.assertEqual(code, "Expected exactly one candidate recipient manifest")
                self.assertEqual(calls, [])

    def test_cloud_shell_example_parses_without_executing_publish(self):
        bash = shutil.which("bash")
        if not bash:
            installed = Path(r"C:\Program Files\Git\bin\bash.exe")
            bash = str(installed) if installed.is_file() else None
        if not bash:
            self.skipTest("Bash syntax checker is not installed")
        fence = chr(96) * 3
        blocks = re.findall(fence + r"bash\n(.*?)\n" + fence, TEXT, re.S)
        self.assertTrue(blocks)
        result = subprocess.run([bash, "-n"], input="\n".join(blocks),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_powershell_examples_parse_without_executing_operations(self):
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            self.skipTest("PowerShell syntax checker is not installed")
        fence = chr(96) * 3
        blocks = re.findall(fence + r"powershell\n(.*?)\n" + fence, TEXT, re.S)
        self.assertTrue(blocks)
        parser = (
            "$docText = [Console]::In.ReadToEnd();"
            "$docTokens = $null; $docErrors = $null;"
            "[System.Management.Automation.Language.Parser]::ParseInput("
            "$docText, [ref]$docTokens, [ref]$docErrors) | Out-Null;"
            "if ($docErrors.Count) { $docErrors | ForEach-Object Message; exit 1 }"
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", parser],
            input="\n".join(blocks), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
