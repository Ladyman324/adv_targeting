"""Publish webapp/ to Azure Static Web Apps.

WHY THIS EXISTS RATHER THAN A GITHUB ACTION
--------------------------------------------
The obvious way to deploy a Static Web App is to point it at a repository and
let CI build it. That is wrong here: webapp/data holds ~175 MB of JSON that is
regenerated on every rebuild. Committing it would bloat the repository
permanently, make every clone painful, and put a quarter of a million advisor
contact records into version control for no benefit. So the app is pushed
straight from the build machine and the data never enters git.

WHAT IT EXCLUDES, AND WHY THAT MATTERS
--------------------------------------
The .gz sidecars written by web_assets.py are DELIBERATELY not uploaded. They
exist because serve.py hands out pre-compressed files on the internal network;
a CDN compresses on the fly, so uploading them would waste 34 MB of a 500 MB
per-deployment budget and risk the platform serving contacts.json.gz as a file
rather than as an encoding.

DEPLOYMENT MODE
---------------
    --full   stage and publish the complete static web application. This flag
             is required so production deployment is an explicit operation.

    swa login --app-name <static-app-name> --use-keychain
    python src/deploy_swa.py --full --static-only \
        --static-app <static-app-name>

This script never publishes api/. The repository contains a queue-triggered
Function that a managed Static Web Apps API cannot run, so deployments must use
--static-only. API releases use the verified artifact built by src/build_api.sh.

The preferred authentication is the SWA CLI's native Windows keychain or its
documented SWA_CLI_DEPLOYMENT_TOKEN environment variable.  The legacy
SWA_DEPLOYMENT_TOKEN environment variable remains an environment-only fallback;
tokens are never accepted in this process's arguments or placed in child
arguments or logs.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = ROOT / "webapp"
API = ROOT / "api"

# Never uploaded. Pre-compressed sidecars are redundant behind a CDN, and the
# manifest describes a delivery mechanism that does not apply there.
EXCLUDE_SUFFIX = {".gz", ".tmp"}
# contacts.json is a BUILD artefact, not a deployed one.
#
# It is 40 MB and Static Web Apps refuses anything over 25 -- with its own 500
# page, no Content-Length, and nothing in any log naming the file. The map still
# drew, because the loader fell through to an empty advisor set, so every card
# showed no phone and no email. A confident wrong answer.
#
# The browser now reads contacts_base.json plus its shards, all comfortably
# under the limit. The whole file stays on disk because act_crosswalk,
# export_advisor_emails and the audit all read it -- it simply must not ship.
EXCLUDE_NAMES = {".gzip_manifest.json", "contacts.json"}
FORBIDDEN_DEPLOY_NAMES = {
    ".git-credentials",
    ".netrc",
    ".npmrc",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_ecdsa",
    "id_rsa",
    "local.settings.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
}
FORBIDDEN_DEPLOY_SUFFIXES = {
    ".7z",
    ".bak",
    ".backup",
    ".cer",
    ".crt",
    ".der",
    ".jks",
    ".kdbx",
    ".key",
    ".keystore",
    ".old",
    ".orig",
    ".p12",
    ".pem",
    ".pfx",
    ".pkcs12",
    ".ppk",
    ".publishsettings",
    ".rar",
    ".swo",
    ".swp",
    ".tar",
    ".tgz",
    ".zip",
}
FORBIDDEN_DEPLOY_ENDINGS = (
    ".secrets.json",
    ".tar.bz2",
    ".tar.gz",
    ".tar.xz",
)
SAFE_CLI_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

def included(path: pathlib.Path) -> bool:
    return (path.suffix not in EXCLUDE_SUFFIX
            and path.name not in EXCLUDE_NAMES
            and not any(part.startswith(".") for part in path.parts))


def forbidden_deployment_reason(path: pathlib.Path) -> str | None:
    """Explain why a source path must stop deployment, if it must."""
    name = path.name.lower()
    if path.is_symlink():
        return "symbolic links are not allowed"
    if name == ".env" or name.startswith(".env."):
        return "environment files may contain credentials"
    if name.startswith("local.settings.") and name.endswith(".json"):
        return "local settings may contain credentials"
    if name in FORBIDDEN_DEPLOY_NAMES:
        return "local settings may contain credentials"
    if path.suffix.lower() in FORBIDDEN_DEPLOY_SUFFIXES:
        return "backup, archive, key, or certificate artifact"
    if name.endswith(FORBIDDEN_DEPLOY_ENDINGS):
        return "archive artifact"
    return None


def validate_deployment_source(
    root: pathlib.Path = WEB,
    *,
    excluded_names: set[str] | None = None,
    excluded_suffixes: set[str] | None = None,
) -> None:
    """Fail closed before copying a risky artifact into the deploy tree."""
    ignored_names = {name.lower() for name in (excluded_names or set())}
    ignored_suffixes = {
        suffix.lower() for suffix in (excluded_suffixes or set())
    }
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part.lower() in ignored_names for part in rel.parts):
            continue
        if path.suffix.lower() in ignored_suffixes:
            continue
        reason = forbidden_deployment_reason(path)
        if reason:
            raise RuntimeError(f"refusing to deploy {rel}: {reason}")


def cleanup_staged_tree(path: pathlib.Path, expected_prefix: str) -> None:
    """Remove only an expected direct child of the system temp directory."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        raise RuntimeError(f"refusing to recursively remove symlink {path}")
    temp_root = pathlib.Path(tempfile.gettempdir()).resolve()
    candidate = path.absolute()
    if (candidate.parent.resolve() != temp_root
            or not candidate.name.startswith(expected_prefix)
            or candidate == temp_root):
        raise RuntimeError(
            f"refusing to remove unverified staging directory {path}"
        )
    shutil.rmtree(candidate)


def validated_cli_label(value: str | None, option: str) -> str | None:
    """Accept Azure-style identifiers without command-shell metacharacters."""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or not SAFE_CLI_LABEL.fullmatch(normalized):
        raise ValueError(
            f"{option} must use only letters, numbers, '.', '_' and '-'"
        )
    return normalized


def reject_token_arguments(argv: list[str]) -> None:
    """Reject the removed token option without echoing its secret value."""
    if any(arg == "--token" or arg.startswith("--token=") for arg in argv):
        sys.exit(
            "[!] --token is not accepted because command arguments may be "
            "recorded. Use SWA_CLI_DEPLOYMENT_TOKEN or the SWA CLI keychain."
        )


def check_config() -> None:
    """Refuse to deploy a config that would lock everyone out or let everyone in."""
    cfg_path = WEB / "staticwebapp.config.json"
    if not cfg_path.exists():
        sys.exit("[!] webapp/staticwebapp.config.json is missing -- the site "
                 "would deploy with NO authentication at all.")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    issuer = (cfg.get("auth", {}).get("identityProviders", {})
                 .get("azureActiveDirectory", {}).get("registration", {})
                 .get("openIdIssuer", ""))
    if "REPLACE_WITH_TENANT_ID" in issuer:
        sys.exit("[!] staticwebapp.config.json still contains "
                 "REPLACE_WITH_TENANT_ID -- fill in the Directory (tenant) ID "
                 "before deploying, or nobody will be able to sign in.")
    routes = cfg.get("routes", [])
    # The one line that stops contacts.json being world-readable. Checked here
    # because the site LOOKS correct whether or not it is present.
    guarded = [r for r in routes
               if r.get("route") in ("/*", "/data/*")
               and "authenticated" in (r.get("allowedRoles") or [])]
    if not guarded:
        sys.exit("[!] no route requires the 'authenticated' role -- this would "
                 "publish the contact data to anyone with the URL.")
    print(f"[*] config OK: {len(routes)} route rule(s), tenant issuer set")


def function_bindings(api: pathlib.Path = API) -> dict[str, set[str]]:
    """Return binding types by function without walking node_modules."""
    found: dict[str, set[str]] = {}
    if not (api / "host.json").exists():
        return found
    for child in api.iterdir():
        config = child / "function.json"
        if not child.is_dir() or not config.exists():
            continue
        try:
            definition = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            sys.exit(f"[!] cannot read {config}: {exc}")
        found[child.name] = {
            str(binding.get("type", "")).strip()
            for binding in definition.get("bindings", [])
            if binding.get("type")
        }
    return found


def requires_linked_function_app(bindings: dict[str, set[str]]) -> bool:
    """Managed SWA Functions can contain only HTTP trigger functions."""
    return any(
        binding.endswith("Trigger") and binding != "httpTrigger"
        for types in bindings.values()
        for binding in types
    )


def resolve_cli_command(cmd: list[str]) -> list[str]:
    """Resolve a CLI executable without relying on ``shell=True``.

    npm installs both a POSIX extensionless shim and a Windows ``.cmd`` shim.
    ``shutil.which('swa')`` can select the former on Windows, where it is not a
    native executable.  Prefer the Windows launchers explicitly and invoke
    batch shims through ``cmd.exe`` while keeping ``shell=False``.
    """
    if not cmd:
        raise ValueError("command must not be empty")

    executable: str | None = None
    if os.name == "nt" and pathlib.Path(cmd[0]).suffix == "":
        for suffix in (".exe", ".com", ".cmd", ".bat"):
            executable = shutil.which(cmd[0] + suffix)
            if executable:
                break
    if not executable:
        executable = shutil.which(cmd[0])
    if not executable:
        raise FileNotFoundError(cmd[0])

    resolved = [executable, *cmd[1:]]
    if os.name == "nt" and pathlib.Path(executable).suffix.lower() in {
            ".cmd", ".bat"}:
        command_processor = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
        if not command_processor:
            raise FileNotFoundError("cmd.exe")
        return [command_processor, "/d", "/s", "/c", *resolved]
    return resolved


def run_checked(
    cmd: list[str],
    *,
    cwd: pathlib.Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Run a CLI command directly, resolving npm's Windows wrappers safely."""
    subprocess.run(
        resolve_cli_command(cmd),
        check=True,
        cwd=cwd,
        env=env,
        shell=False,
    )


def swa_child_environment(token: str | None) -> dict[str, str]:
    """Build an isolated child environment without putting a token in argv."""
    child = os.environ.copy()
    # The legacy name is for this Python wrapper only.  The CLI's documented
    # environment variable is SWA_CLI_DEPLOYMENT_TOKEN.
    child.pop("SWA_DEPLOYMENT_TOKEN", None)
    if token:
        child["SWA_CLI_DEPLOYMENT_TOKEN"] = token
    return child


def swa_deploy_command(
    folder: pathlib.Path,
    *,
    environment: str,
    static_app: str | None,
) -> list[str]:
    """Build the non-secret portion of the SWA CLI command."""
    cmd = ["swa", "deploy", str(folder), "--env", environment]
    if static_app:
        cmd += ["--app-name", static_app]
    return cmd


def stage() -> pathlib.Path:
    """Copy what should be uploaded into a temporary folder."""
    validate_deployment_source()
    out = pathlib.Path(tempfile.mkdtemp(prefix="swa_deploy_"))
    try:
        kept = skipped = 0
        total = 0
        for src in WEB.rglob("*"):
            # Repeat the critical checks at copy time in case the source tree
            # changed after preflight validation.
            reason = forbidden_deployment_reason(src)
            if reason:
                rel = src.relative_to(WEB)
                raise RuntimeError(f"refusing to deploy {rel}: {reason}")
            if not src.is_file() or not included(src):
                skipped += 1
                continue
            rel = src.relative_to(WEB)
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            kept += 1
            total += dest.stat().st_size

        print(f"[*] staged {kept:,} files "
              f"({total / 1e6:.1f} MB), skipped {skipped:,}")
        return out
    except BaseException:
        cleanup_staged_tree(out, "swa_deploy_")
        raise


SETUP_HELP = """
Deployment responsibilities
===========================
This script deploys webapp/ only. It never stages or publishes api/.

Static Web App:
  python src/deploy_swa.py --full --static-only --static-app <static-app-name>

Authenticate with SWA_CLI_DEPLOYMENT_TOKEN or run once:
  swa login --app-name <static-app-name> --use-keychain

Function App API:
  1. Commit all intended source changes; the canonical builder refuses a dirty
     worktree. Ensure the ignored generated ACT lookup files are present and
     current locally; the builder validates them without committing them.
  2. Use Node 22.12 or newer for source tests and packaging. The Azure
     Functions runtime must be Node 22.
  3. Restore dependencies, then build and verify the provenance-stamped
     immutable artifact from PowerShell on this workstation:
       npm ci --prefix api
       & 'C:\\Program Files\\Git\\bin\\bash.exe' src/build_api.sh
  4. Upload dist/api.tgz and dist/api.tgz.sha256 to a fresh Cloud Shell.
  5. Follow docs/deployment_automation.md to verify the uploaded hash, extract
     into a newly created temporary directory, publish that exact verified
     package, and confirm its release provenance through /api/health.

The builder runs the API tests, validates ACT lookup artifacts, rejects
secrets/links/backups, stamps release provenance, and emits the retained
SHA-256. Do not replace it with a direct api/ folder upload.

See docs/deployment_automation.md for the canonical release commands.
"""

def main() -> None:
    reject_token_arguments(sys.argv[1:])
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # Not `required=True`: --setup-help answers a question you have BEFORE you
    # have decided what to deploy, and demanding --full to read it would
    # be a small piece of nonsense.
    ap.add_argument(
        "--full", action="store_true", help="the whole static web application"
    )
    ap.add_argument(
        "--static-app",
        default=os.environ.get("AZURE_STATIC_WEB_APP_NAME"),
        help=("Static Web App name used to select keychain credentials (or "
              "set AZURE_STATIC_WEB_APP_NAME)"),
    )
    ap.add_argument(
        "--env",
        dest="deployment_environment",
        default=os.environ.get("SWA_DEPLOYMENT_ENV", "production"),
        help=("SWA deployment environment; defaults explicitly to "
              "production (or set SWA_DEPLOYMENT_ENV)"),
    )
    ap.add_argument(
        "--static-only",
        action="store_true",
        help=("required safety acknowledgement: upload webapp/ only; API "
              "releases use src/build_api.sh and Cloud Shell"),
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="stage and report, but do not upload")
    ap.add_argument("--setup-help", action="store_true",
                    help="print the canonical static/API deployment split")
    args = ap.parse_args()

    if args.setup_help:
        print(SETUP_HELP)
        return
    if not args.full:
        ap.error("--full is required (or --setup-help)")
    if not args.static_only:
        ap.error("--static-only is required; this script never deploys api/")
    try:
        args.deployment_environment = validated_cli_label(
            args.deployment_environment, "--env"
        )
        args.static_app = validated_cli_label(args.static_app, "--static-app")
    except ValueError as exc:
        ap.error(str(exc))

    check_config()
    bindings = function_bindings()
    linked_required = requires_linked_function_app(bindings)
    queue_functions = sorted(
        name for name, types in bindings.items() if "queueTrigger" in types
    )
    if linked_required:
        detail = f" ({', '.join(queue_functions)})" if queue_functions else ""
        print("[*] linked Function App required by non-HTTP trigger(s)" + detail)
    elif bindings:
        print(f"[*] managed API is compatible: {len(bindings)} HTTP function(s)")

    legacy_env_token = os.environ.get("SWA_DEPLOYMENT_TOKEN")
    token = legacy_env_token
    if legacy_env_token:
        print("[!] SWA_DEPLOYMENT_TOKEN is a legacy fallback; prefer the SWA "
              "CLI native keychain or SWA_CLI_DEPLOYMENT_TOKEN")
    elif os.environ.get("SWA_CLI_DEPLOYMENT_TOKEN"):
        print("[*] using the SWA CLI deployment-token environment fallback")
    else:
        print("[*] using SWA CLI native keychain authentication")

    folder = stage()
    try:
        if args.dry_run:
            print("[*] dry run -- API deployment intentionally excluded")
            print(f"[*] dry run -- staged and verified {folder}")
            return

        cmd = swa_deploy_command(
            folder,
            environment=args.deployment_environment,
            static_app=args.static_app,
        )
        print("[*] Static Web App upload excludes api/; API releases use the "
              "verified src/build_api.sh artifact")

        target = args.static_app or "the app saved by `swa login`"
        print(f"[*] deploying {target!r} to SWA environment "
              f"{args.deployment_environment!r}")
        try:
            run_checked(cmd, env=swa_child_environment(token))
        except FileNotFoundError:
            sys.exit("[!] the SWA CLI is not installed. Run:\n"
                     "    npm install -g @azure/static-web-apps-cli")
        except subprocess.CalledProcessError as exc:
            app_arg = (f" --app-name {args.static_app}"
                       if args.static_app else "")
            sys.exit(
                f"[!] Static Web App deployment failed (exit "
                f"{exc.returncode}).\n"
                "    If authentication is not configured, run once:\n"
                f"    swa login{app_arg} --use-keychain"
            )
    finally:
        cleanup_staged_tree(folder, "swa_deploy_")


if __name__ == "__main__":
    main()
