"""Regression imports outside src must survive isolated code capture."""
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from refresh_core import RefreshError, code_files, normalized_code_hash


class OfflineTestDependencyCaptureTests(unittest.TestCase):
    helper = "data/refresh/campaigns/all-rosters-20261025/audit_edward_jones_email_concordance.py"
    test = "tests/test_edward_jones_concordance.py"

    def write(self, root, relative, text):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_capture_supplies_the_imported_helper_and_binds_its_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, self.test, "# regression module")
            self.write(root, self.helper, "EVIDENCE = 1")
            with tempfile.TemporaryDirectory() as destination:
                stage = Path(destination)
                for source in code_files(root):
                    target = stage / source.relative_to(root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(source), str(target))
                self.assertEqual((stage / self.helper).read_text(), "EVIDENCE = 1")
                first = normalized_code_hash(stage / self.helper, self.helper)
                (stage / self.helper).write_text("EVIDENCE = 2")
                self.assertNotEqual(first, normalized_code_hash(stage / self.helper, self.helper))

    def test_missing_import_dependency_fails_before_the_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, self.test, "# regression module")
            with self.assertRaisesRegex(RefreshError, "Required offline test helper"):
                code_files(root)

    def test_campaign_data_and_unused_helpers_are_not_broadly_copied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, self.test, "# regression module")
            helper = self.write(root, self.helper, "EVIDENCE = 1")
            for name in ["contacts.json", "arbitrary.py"]:
                helper.with_name(name).write_text("private data")
            self.assertEqual(
                {path.relative_to(root).as_posix() for path in code_files(root)},
                {self.test, self.helper},
            )

    def test_unrelated_fixture_does_not_require_archived_campaigns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.write(root, "src/module.py", "VALUE = 1")
            self.assertEqual(code_files(root), [source])


if __name__ == "__main__":
    unittest.main()
