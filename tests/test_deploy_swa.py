"""Focused security and lifecycle tests for src/deploy_swa.py."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "deploy_swa", ROOT / "src" / "deploy_swa.py"
)
assert SPEC and SPEC.loader
deploy_swa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy_swa)


class CommandTests(unittest.TestCase):
    def test_web_asset_check_uses_current_python_without_shell(self):
        with mock.patch.object(deploy_swa.subprocess, "run") as run:
            deploy_swa.check_web_assets()

        run.assert_called_once_with(
            [
                sys.executable,
                str(ROOT / "src" / "web_assets.py"),
                "--check",
            ],
            check=True,
            cwd=ROOT,
            shell=False,
        )

    def test_web_asset_check_reports_fail_closed_result(self):
        failure = subprocess.CalledProcessError(7, ["python", "--check"])
        with mock.patch.object(
            deploy_swa.subprocess, "run", side_effect=failure
        ), self.assertRaisesRegex(
            SystemExit, "nothing was staged or deployed"
        ):
            deploy_swa.check_web_assets()

    def test_cli_labels_reject_command_metacharacters(self):
        with self.assertRaises(ValueError):
            deploy_swa.validated_cli_label("production & whoami", "--env")

    def test_deploy_command_contains_target_but_never_token_argument(self):
        command = deploy_swa.swa_deploy_command(
            pathlib.Path("C:/staged"),
            environment="preview-blue",
            static_app="eic-advisors",
        )

        self.assertEqual(
            command,
            [
                "swa", "deploy", "C:\\staged", "--env", "preview-blue",
                "--app-name", "eic-advisors",
            ],
        )
        self.assertNotIn("--deployment-token", command)

    def test_legacy_token_is_mapped_only_to_cli_environment(self):
        with mock.patch.dict(
            os.environ,
            {
                "SWA_DEPLOYMENT_TOKEN": "legacy-secret",
                "SWA_CLI_DEPLOYMENT_TOKEN": "older-secret",
                "KEEP_ME": "yes",
            },
            clear=True,
        ):
            child = deploy_swa.swa_child_environment("replacement-secret")

            self.assertNotIn("SWA_DEPLOYMENT_TOKEN", child)
            self.assertEqual(
                child["SWA_CLI_DEPLOYMENT_TOKEN"], "replacement-secret"
            )
            self.assertEqual(child["KEEP_ME"], "yes")
            self.assertEqual(os.environ["SWA_DEPLOYMENT_TOKEN"], "legacy-secret")

    def test_documented_cli_token_environment_is_preserved(self):
        with mock.patch.dict(
            os.environ,
            {"SWA_CLI_DEPLOYMENT_TOKEN": "documented-secret"},
            clear=True,
        ):
            child = deploy_swa.swa_child_environment(None)

        self.assertEqual(
            child["SWA_CLI_DEPLOYMENT_TOKEN"], "documented-secret"
        )

    @unittest.skipUnless(os.name == "nt", "Windows npm-wrapper behavior")
    def test_npm_cmd_wrapper_is_explicit_and_shell_free(self):
        def which(name):
            return {
                "swa.cmd": r"C:\\npm\\swa.cmd",
            }.get(name)

        with mock.patch.object(deploy_swa.shutil, "which", side_effect=which), \
                mock.patch.dict(os.environ, {"COMSPEC": r"C:\\Windows\\cmd.exe"}):
            resolved = deploy_swa.resolve_cli_command(
                ["swa", "deploy", r"C:\\staged"]
            )

        self.assertEqual(
            resolved,
            [
                r"C:\\Windows\\cmd.exe", "/d", "/s", "/c",
                r"C:\\npm\\swa.cmd", "deploy", r"C:\\staged",
            ],
        )

    def test_run_checked_always_disables_subprocess_shell(self):
        with mock.patch.object(
            deploy_swa, "resolve_cli_command", return_value=["resolved"]
        ), mock.patch.object(deploy_swa.subprocess, "run") as run:
            deploy_swa.run_checked(["swa"], env={"SAFE": "1"})

        run.assert_called_once_with(
            ["resolved"], check=True, cwd=None, env={"SAFE": "1"}, shell=False
        )


class StagingSafetyTests(unittest.TestCase):
    def _write_valid_staged_tree(self, staged):
        data = staged / "data"
        data.mkdir()
        (data / "metadata.json").write_text(
            '{"generated_utc":"2026-08-23T12:00:00+00:00"}',
            encoding="utf-8",
        )
        payload = data / "pins_test.json"
        payload.write_text('{"pins":[1]}', encoding="utf-8")

        spec = importlib.util.spec_from_file_location(
            "test_web_assets", ROOT / "src" / "web_assets.py"
        )
        assert spec and spec.loader
        web_assets = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(web_assets)
        expected = web_assets.data_fingerprint(data)
        (staged / "app.js").write_text(
            f'const DATA_VERSION = "{expected}";', encoding="utf-8"
        )
        (staged / "field.js").write_text(
            f'const DATA_VERSION = "{expected}";', encoding="utf-8"
        )
        for name in (
            "style.css", "field.css", "dial.js", "email.js", "email.css"
        ):
            (staged / name).write_text(f"/* {name} */", encoding="utf-8")
        (staged / "sw.js").write_text(
            'const VERSION = "";', encoding="utf-8"
        )

        web_assets.WEB = staged
        desktop_tag = web_assets.asset_tag()
        field_tag = web_assets.field_tag()
        (staged / "index.html").write_text(
            " ".join(
                f'"{name}?v={desktop_tag}"'
                for name in web_assets.VERSIONED
            ),
            encoding="utf-8",
        )
        (staged / "field.html").write_text(
            " ".join(
                f'"{name}?v={field_tag}"'
                for name in web_assets.FIELD_VERSIONED
            ),
            encoding="utf-8",
        )
        (staged / "sw.js").write_text(
            f'const VERSION = "{field_tag}";', encoding="utf-8"
        )
        return payload

    def test_stage_verifies_the_completed_copy_before_returning(self):
        with tempfile.TemporaryDirectory() as temp:
            source = pathlib.Path(temp) / "webapp"
            source.mkdir()
            (source / "app.js").write_text("// app", encoding="utf-8")
            with mock.patch.object(deploy_swa, "WEB", source), \
                    mock.patch.object(
                        deploy_swa, "validate_deployment_source"
                    ), mock.patch.object(
                        deploy_swa, "check_staged_web_assets"
                    ) as staged_check:
                folder = deploy_swa.stage()
        try:
            staged_check.assert_called_once_with(folder)
        finally:
            deploy_swa.cleanup_staged_tree(folder, "swa_deploy_")

    def test_hybrid_staged_data_cannot_pass_fingerprint_check(self):
        with tempfile.TemporaryDirectory() as temp:
            staged = pathlib.Path(temp)
            payload = self._write_valid_staged_tree(staged)
            deploy_swa.check_staged_web_assets(staged)
            payload.write_text('{"pins":[1,2]}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "mixed during copy"):
                deploy_swa.check_staged_web_assets(staged)

    def test_stale_desktop_pin_cannot_pass_staged_check(self):
        for target in ("index.html", "app.js"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp:
                staged = pathlib.Path(temp)
                self._write_valid_staged_tree(staged)
                path = staged / target
                if target == "index.html":
                    path.write_text(
                        path.read_text(encoding="utf-8").replace(
                            "app.js?v=", "app.js?v=stale-", 1
                        ),
                        encoding="utf-8",
                    )
                else:
                    path.write_text(
                        path.read_text(encoding="utf-8") + "\n// changed",
                        encoding="utf-8",
                    )
                with self.assertRaisesRegex(RuntimeError, "app.js"):
                    deploy_swa.check_staged_web_assets(staged)

    def test_stale_field_pin_and_worker_tag_cannot_pass_staged_check(self):
        for target in ("field.html", "sw.js"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp:
                staged = pathlib.Path(temp)
                self._write_valid_staged_tree(staged)
                path = staged / target
                if target == "field.html":
                    path.write_text(
                        path.read_text(encoding="utf-8").replace(
                            "field.js?v=", "field.js?v=stale-", 1
                        ),
                        encoding="utf-8",
                    )
                else:
                    path.write_text(
                        'const VERSION = "stale";', encoding="utf-8"
                    )
                with self.assertRaisesRegex(RuntimeError, target.split(".")[0]):
                    deploy_swa.check_staged_web_assets(staged)

    def test_sensitive_backup_archive_and_key_artifacts_fail_closed(self):
        forbidden = [
            ".env",
            ".env.production",
            ".npmrc",
            "credentials.json",
            "prod.secrets.json",
            "local.settings.json",
            "local.settings.development.json",
            "settings.bak",
            "release.zip",
            "release.tgz",
            "release.tar",
            "release.tar.gz",
            "private.key",
            "private.pem",
            "private.pfx",
            "private.p12",
            "azure.publishsettings",
            "passwords.kdbx",
            "id_ecdsa",
        ]
        for name in forbidden:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = pathlib.Path(temp)
                (root / name).write_text("not-a-real-secret", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "refusing to deploy"):
                    deploy_swa.validate_deployment_source(root)

    def test_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            (root / "linked.json").symlink_to(target)

            with self.assertRaisesRegex(RuntimeError, "symbolic links"):
                deploy_swa.validate_deployment_source(root)

    def test_normal_generated_json_remains_included(self):
        path = pathlib.Path("data") / "pins_ga.json"

        self.assertIsNone(deploy_swa.forbidden_deployment_reason(path))
        self.assertTrue(deploy_swa.included(path))

    def test_existing_gzip_sidecar_remains_excluded_not_forbidden(self):
        path = pathlib.Path("data") / "pins_ga.json.gz"

        self.assertIsNone(deploy_swa.forbidden_deployment_reason(path))
        self.assertFalse(deploy_swa.included(path))

    def test_hidden_directory_contents_are_excluded(self):
        self.assertFalse(
            deploy_swa.included(pathlib.Path("data") / ".private" / "file.json")
        )

    def test_cleanup_removes_only_verified_temp_tree(self):
        folder = pathlib.Path(tempfile.mkdtemp(prefix="swa_deploy_"))
        (folder / "file.txt").write_text("x", encoding="utf-8")

        deploy_swa.cleanup_staged_tree(folder, "swa_deploy_")

        self.assertFalse(folder.exists())

    def test_cleanup_refuses_unexpected_temp_tree_name(self):
        folder = pathlib.Path(tempfile.mkdtemp(prefix="not_a_deploy_"))
        try:
            with self.assertRaisesRegex(RuntimeError, "unverified staging"):
                deploy_swa.cleanup_staged_tree(folder, "swa_deploy_")
            self.assertTrue(folder.exists())
        finally:
            folder.rmdir()


class MainTests(unittest.TestCase):
    def _run_main(self, argv, environment=None):
        folder = pathlib.Path(tempfile.mkdtemp(prefix="swa_deploy_"))
        output = io.StringIO()
        environment = environment or {}
        with mock.patch.object(deploy_swa, "check_web_assets") as assets, \
                mock.patch.object(deploy_swa, "check_config"), \
                mock.patch.object(deploy_swa, "function_bindings", return_value={}), \
                mock.patch.object(deploy_swa, "stage", return_value=folder), \
                mock.patch.object(deploy_swa, "run_checked") as run, \
                mock.patch.object(sys, "argv", ["deploy_swa.py", *argv]), \
                mock.patch.dict(os.environ, environment, clear=True), \
                contextlib.redirect_stdout(output):
            deploy_swa.main()
        return folder, run, assets, output.getvalue()

    def test_dry_run_removes_staging_folder(self):
        folder, run, assets, output = self._run_main(
            ["--full", "--static-only", "--dry-run"]
        )

        self.assertFalse(folder.exists())
        run.assert_not_called()
        assets.assert_called_once_with()
        self.assertIn("staged and verified at", output)

    def test_keychain_path_needs_no_token(self):
        folder, run, assets, output = self._run_main(
            [
                "--full", "--static-only", "--static-app", "eic-advisors",
                "--env", "preview-blue",
            ]
        )

        self.assertFalse(folder.exists())
        assets.assert_called_once_with()
        command = run.call_args.args[0]
        child_environment = run.call_args.kwargs["env"]
        self.assertNotIn("--deployment-token", command)
        self.assertEqual(command[-4:], ["--env", "preview-blue", "--app-name", "eic-advisors"])
        self.assertNotIn("SWA_CLI_DEPLOYMENT_TOKEN", child_environment)
        self.assertIn("native keychain authentication", output)

    def test_environment_defaults_explicitly_to_production(self):
        folder, run, assets, _ = self._run_main(["--full", "--static-only"])

        self.assertFalse(folder.exists())
        assets.assert_called_once_with()
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["--env", "production"])

    def test_static_only_acknowledgement_is_required(self):
        with mock.patch.object(sys, "argv", ["deploy_swa.py", "--full"]), \
                mock.patch.object(deploy_swa, "stage") as stage, \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as raised:
            deploy_swa.main()

        self.assertEqual(raised.exception.code, 2)
        stage.assert_not_called()

    def test_removed_function_deployment_options_are_rejected(self):
        for removed in (
            ["--function-app", "eic-advisors-api"],
            ["--no-remote-build"],
        ):
            with self.subTest(option=removed[0]), mock.patch.object(
                sys,
                "argv",
                ["deploy_swa.py", "--full", "--static-only", *removed],
            ), mock.patch.object(deploy_swa, "stage") as stage, \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                deploy_swa.main()

            self.assertEqual(raised.exception.code, 2)
            stage.assert_not_called()

    def test_no_direct_api_staging_or_publish_entrypoint_remains(self):
        self.assertFalse(hasattr(deploy_swa, "publish_function_app"))
        self.assertFalse(hasattr(deploy_swa, "non_swa_child_environment"))
        self.assertNotIn("--api-location", deploy_swa.swa_deploy_command(
            pathlib.Path("C:/stage"),
            environment="production",
            static_app="eic-advisors",
        ))

    def test_setup_help_points_to_verified_api_artifact(self):
        help_text = deploy_swa.SETUP_HELP
        self.assertIn(
            "& 'C:\\Program Files\\Git\\bin\\bash.exe' src/build_api.sh",
            help_text,
        )
        self.assertIn("dist/api.tgz.sha256", help_text)
        self.assertIn("fresh Cloud Shell", help_text)
        self.assertIn("Node 22.12 or newer", help_text)
        self.assertIn("runtime must be Node 22", help_text)
        self.assertIn(
            "C:\\Program Files\\Git\\bin\\bash.exe", help_text
        )
        self.assertNotIn("upload the api FOLDER", help_text)
        self.assertNotIn("mkdir -p ~/api", help_text)
        self.assertNotIn("tar -xzf ~/api.tgz -C ~/api", help_text)

    def test_setup_help_does_not_run_web_asset_check_or_stage(self):
        output = io.StringIO()
        with mock.patch.object(
            sys, "argv", ["deploy_swa.py", "--setup-help"]
        ), mock.patch.object(deploy_swa, "check_web_assets") as assets, \
                mock.patch.object(deploy_swa, "stage") as stage, \
                contextlib.redirect_stdout(output):
            deploy_swa.main()

        assets.assert_not_called()
        stage.assert_not_called()
        self.assertIn("Deployment responsibilities", output.getvalue())

    def test_failed_web_asset_check_blocks_staging_and_deploy(self):
        failure = SystemExit(
            "[!] web asset integrity check failed; nothing was staged or deployed"
        )
        with mock.patch.object(
            sys,
            "argv",
            ["deploy_swa.py", "--full", "--static-only", "--dry-run"],
        ), mock.patch.object(
            deploy_swa, "check_web_assets", side_effect=failure
        ) as assets, mock.patch.object(deploy_swa, "stage") as stage, \
                mock.patch.object(deploy_swa, "run_checked") as run, \
                self.assertRaisesRegex(SystemExit, "nothing was staged"):
            deploy_swa.main()

        assets.assert_called_once_with()
        stage.assert_not_called()
        run.assert_not_called()

    def test_obsolete_partial_test_mode_is_rejected(self):
        with mock.patch.object(
            sys, "argv", ["deploy_swa.py", "--test", "--dry-run"]
        ), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(
            SystemExit
        ) as raised:
            deploy_swa.main()

        self.assertEqual(raised.exception.code, 2)

    def test_removed_token_option_is_rejected_without_echoing_secret(self):
        secret = "top-secret-value"
        error = io.StringIO()
        with mock.patch.object(
            sys,
            "argv",
            ["deploy_swa.py", "--full", "--token", secret],
        ), contextlib.redirect_stderr(error), self.assertRaises(SystemExit):
            deploy_swa.main()

        self.assertNotIn(secret, error.getvalue())

    def test_documented_environment_token_never_enters_child_argv(self):
        secret = "environment-only-secret"
        folder, run, assets, output = self._run_main(
            ["--full", "--static-only"],
            {"SWA_CLI_DEPLOYMENT_TOKEN": secret},
        )

        self.assertFalse(folder.exists())
        assets.assert_called_once_with()
        command = run.call_args.args[0]
        child_environment = run.call_args.kwargs["env"]
        self.assertNotIn(secret, repr(command))
        self.assertNotIn(secret, output)
        self.assertEqual(child_environment["SWA_CLI_DEPLOYMENT_TOKEN"], secret)


if __name__ == "__main__":
    unittest.main()
