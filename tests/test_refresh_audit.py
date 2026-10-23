"""Audit must recognize threaded follow-up lint without accepting its removal."""
from pathlib import Path
import ast
import re
import types
import unittest
from unittest import mock

# audit.check executes all data audits at import time. Compile the exact target
# function without its decorator so this unit contract never scans live data.
root = Path(__file__).resolve().parents[1]
tree = ast.parse((root / "src/audit.py").read_text(encoding="utf-8"))
function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_merge_safety")
function.decorator_list = []
audit = types.ModuleType("refresh_audit_contract")
audit.API, audit.WEB, audit.re = root / "api", root / "webapp", re
audit.ROOT = root
audit.text = lambda path: path.read_text(encoding="utf-8")
exec(compile(ast.Module(body=[function], type_ignores=[]), "audit_merge_contract", "exec"), audit.__dict__)
settings_function = next(node for node in tree.body
                         if isinstance(node, ast.FunctionDef) and node.name == "_settings_keys")
settings_function.decorator_list = []
exec(compile(ast.Module(body=[settings_function], type_ignores=[]),
             "audit_settings_contract", "exec"), audit.__dict__)


class RefreshAuditTests(unittest.TestCase):
    def test_current_complete_settings_parity_includes_opt_in_photo_key(self):
        ok, detail = audit._settings_keys()
        self.assertTrue(ok, detail)
        serve_tree = ast.parse((root / "serve.py").read_text(encoding="utf-8"))
        assignment = next(node for node in serve_tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "SETTING_KEYS" for target in node.targets))
        keys = ast.literal_eval(assignment.value)
        self.assertEqual(keys["signaturePhotoEnabled"], 1)
        self.assertNotIn("unknownPreference", keys)

    def test_missing_photo_preference_still_fails_the_exact_settings_audit(self):
        original = audit.text
        serve_path = root / "serve.py"
        incomplete = original(serve_path).replace('    "signaturePhotoEnabled": 1,\n', "")
        self.assertNotEqual(incomplete, original(serve_path))
        with mock.patch.object(audit, "text", side_effect=lambda path: incomplete if path == serve_path else original(path)):
            ok, detail = audit._settings_keys()
            self.assertFalse(ok)
            self.assertIn("signaturePhotoEnabled", detail)

    def test_current_threaded_and_normal_subject_lint_are_recognized(self):
        self.assertTrue(audit._merge_safety()[0])
        original = audit.text
        service_path = audit.API / "shared" / "email-service.js"
        service = original(service_path).replace(
            'batch.parentBatchId ? "Inherited thread subject" : subjectTemplate',
            "subjectTemplate")
        with mock.patch.object(audit, "text", side_effect=lambda p: service if p == service_path else original(p)):
            self.assertTrue(audit._merge_safety()[0])

    def test_missing_common_edit_lint_is_still_rejected(self):
        original = audit.text
        service_path = audit.API / "shared" / "email-service.js"
        service = original(service_path).replace("core.lintTemplate({", "core.skippedLintTemplate({")
        with mock.patch.object(audit, "text", side_effect=lambda p: service if p == service_path else original(p)):
            self.assertFalse(audit._merge_safety()[0])


if __name__ == "__main__":
    unittest.main()
