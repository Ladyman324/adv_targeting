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

TWO MODES
---------
    --test   the app shell plus a tiny slice of data. Proves the sign-in chain
             and the /data/* protection in about a minute. Use this FIRST: the
             thing most likely to need a second attempt is the auth config, and
             discovering that after a 175 MB upload is a bad trade.
    --full   everything.

    python src/deploy_swa.py --test --function-app <name> --token <token>
    python src/deploy_swa.py --full --function-app <name> --token <token>

The email sender adds a queue-triggered function. Static Web Apps managed
Functions support HTTP triggers only, so this script publishes api/ to a
linked standalone Function App when --function-app (or
AZURE_FUNCTION_APP_NAME) is supplied. It never tries to put a queue trigger
into the managed API upload.

The token can also come from the SWA_DEPLOYMENT_TOKEN environment variable, so
it need not appear in shell history.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
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
API_EXCLUDE_NAMES = {
    ".git",
    ".vscode",
    "local.settings.json",
    "node_modules",
    "test",
    "tools",
}

# For --test: the smallest set that still exercises everything we need to
# verify. contacts.json must be PRESENT (even trimmed) because the whole point
# is checking that an anonymous request for it is refused.
TEST_DATA_KEEP = {"metadata.json"}
TEST_CONTACTS_ADVISORS = 200


def included(path: pathlib.Path) -> bool:
    return (path.suffix not in EXCLUDE_SUFFIX
            and path.name not in EXCLUDE_NAMES
            and not path.name.startswith("."))


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


def run_checked(cmd: list[str], *, cwd: pathlib.Path | None = None) -> None:
    """Run a CLI command while preserving Windows .cmd compatibility."""
    subprocess.run(cmd, check=True, cwd=cwd, shell=(os.name == "nt"))


def publish_function_app(name: str, remote_build: bool = True) -> None:
    """Publish the full Functions project, including its queue worker.

    THE DEPENDENCIES HAVE TO ARRIVE SOMEHOW, and this is where that goes wrong
    silently. `node_modules` is in API_EXCLUDE_NAMES, and Core Tools does not
    run `npm install` for a JavaScript project -- it zips what it is given. So
    excluding the folder and passing no build flag uploads an app with no
    dependencies at all: every function throws `Cannot find module
    '@azure/data-tables'` on its first invocation, the deployment reports
    success, and the failure only shows up as 500s from a live endpoint.

    Two ways to be correct, and the caller picks:

      remote build (default)   Oryx runs `npm install` on the server from
                               package.json. Clean, small upload, and the
                               documented path -- but LINUX ONLY.
      ship node_modules        Works on a Windows plan too. Safe here because
                               every dependency is pure JavaScript; if a native
                               module is ever added, this stops being portable
                               and the remote build becomes the only option.
    """
    how = "remote build" if remote_build else "uploading node_modules"
    print(f"[*] publishing API and queue worker to Function App {name!r} ({how})")

    exclude = set(API_EXCLUDE_NAMES)
    if not remote_build:
        exclude.discard("node_modules")
        if not (API / "node_modules").exists():
            sys.exit(
                "[!] --no-remote-build ships api/node_modules, and it is not "
                "there. Run `npm install` in api/ first, or drop the flag to "
                "let the server install from package.json."
            )

    staged_api = pathlib.Path(tempfile.mkdtemp(prefix="function_deploy_"))
    try:
        shutil.copytree(
            API,
            staged_api,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(*exclude, "*.log", "*.tmp"),
        )
        cmd = ["func", "azure", "functionapp", "publish", name]
        if remote_build:
            cmd += ["--build", "remote"]
        run_checked(cmd, cwd=staged_api)
    except FileNotFoundError:
        sys.exit(
            "[!] Azure Functions Core Tools v4 is not installed. "
            "Install it before using --function-app."
        )
    except subprocess.CalledProcessError as exc:
        sys.exit(
            f"[!] Function App deployment failed (exit {exc.returncode}). "
            "The Static Web App was not changed. Confirm Azure Functions Core "
            "Tools v4 is installed, Azure CLI/PowerShell is signed in, and the "
            "Function App already exists.\n"
            "    If the log mentions remote build or Oryx, the Function App is "
            "probably on Windows — retry with --no-remote-build."
        )
    finally:
        shutil.rmtree(staged_api, ignore_errors=True)


def stage(mode: str) -> pathlib.Path:
    """Copy what should be uploaded into a temporary folder."""
    out = pathlib.Path(tempfile.mkdtemp(prefix="swa_deploy_"))
    kept = skipped = 0
    total = 0
    for src in WEB.rglob("*"):
        if not src.is_file() or not included(src):
            skipped += 1
            continue
        rel = src.relative_to(WEB)
        if mode == "test" and rel.parts and rel.parts[0] == "data":
            if rel.name not in TEST_DATA_KEEP and rel.name != "contacts.json":
                skipped += 1
                continue
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        kept += 1
        total += dest.stat().st_size

    if mode == "test":
        # A trimmed contacts.json, so the protection test has something real to
        # ask for without uploading 36 MB to find out the auth config is wrong.
        cpath = out / "data" / "contacts.json"
        if cpath.exists():
            data = json.loads(cpath.read_text(encoding="utf-8"))
            ids = list(data.get("advisors", {}))[:TEST_CONTACTS_ADVISORS]
            data["advisors"] = {k: data["advisors"][k] for k in ids}
            data["teams"] = {}
            data["practices"] = {}
            data["note"] = "TRIMMED TEST DEPLOYMENT -- not the full dataset"
            total -= cpath.stat().st_size
            cpath.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            total += cpath.stat().st_size

    print(f"[*] staged {kept:,} files ({total / 1e6:.1f} MB), skipped {skipped:,}")
    return out


SETUP_HELP = """
Moving /api onto a linked Function App
======================================
Needed because api/email-worker is a QUEUE trigger. A managed Static Web Apps
API runs HTTP triggers only, so the worker has to live somewhere that can run
it -- and once /api is a linked backend, ALL of /api moves there together.

Done once. After this, deployment is a single command.

  0. Prerequisites -- NONE OF WHICH NEED LOCAL ADMIN
     Written this way because the workstation this ships from does not have it:
     `winget install Microsoft.AzureCLI` downloads, then dies with exit 1602
     (ERROR_INSTALL_USEREXIT) at the elevation prompt. Both Azure MSIs do.

     swa and func -- installed under your own profile, no elevation:

       npm install -g @azure/static-web-apps-cli
       npm install -g azure-functions-core-tools@4 \\
         --allow-scripts azure-functions-core-tools

     THE --allow-scripts FLAG IS THE WHOLE POINT. npm 11 blocks package
     install scripts by default, and Core Tools downloads its actual binary in
     a postinstall. Without the flag npm says "added 35 packages", `func`
     appears on PATH, and running it fails with ENOENT on a binary that was
     never fetched -- installed-looking and non-functional, which is the worst
     of the three possible outcomes. Naming the single package is deliberate;
     --dangerously-allow-all-scripts exists and should not be used here.

     If policy forbids install scripts entirely, take the portable build
     instead -- a plain zip, no installer, no admin:
       https://github.com/Azure/azure-functions-core-tools/releases
     Extract it and put that folder on PATH for the session.

     (npm warns about keytar under the SWA CLI too. Ignore it: keytar only
     caches `swa login` credentials, and this script uses --deployment-token.)

     AZURE CREDENTIALS -- READ THIS BEFORE PLANNING THE DEPLOY.

     `func azure functionapp publish` does NOT authenticate on its own. It
     needs either the Azure CLI or the Az.Accounts PowerShell module signed in
     locally. On a locked-down workstation you may have neither available:

       az CLI          MSI requires local admin (winget dies with 1602)
       az via pip      needs Python >= 3.9; this machine has 3.8
       Az.Accounts     Install-Module needs PSGallery, which may be blocked

     Check before committing to a plan:

       az version                                  # or:
       Get-Module -ListAvailable Az.Accounts

     If BOTH are missing, the API cannot be published from this machine at all
     and there is no flag here that changes that. Use the Cloud Shell route in
     step 4b instead -- it needs nothing installed locally.

     The STATIC SITE is unaffected either way: `swa deploy` authenticates with
     a deployment token, not with Azure credentials, so webapp-only releases
     always work from here.

     Confirm what you have:
       func --version ; swa --version

     The Static Web App must be on the STANDARD plan. Linked backends are not
     available on Free, and this is the step most likely to stop you. Check it
     in the portal under the Static Web App -> Overview -> Plan.

  1. Create the Function App. Portal, or Cloud Shell.

     Hosting plan: choose FLEX CONSUMPTION. It is Microsoft's recommended plan
     for new serverless Functions, uses a quota separate from the classic
     App Service Total VMs meter, and supports Node 22 plus queue triggers.
     Flex is Linux-only. Select a supported region, On Demand, and zero Always
     Ready instances.

     Before linking, run az staticwebapp backends validate. Microsoft's
     overview says existing Function Apps can use any plan, but an older plan
     comparison table does not list Flex. Validation is the safe source of
     truth for this subscription and resource; do not move /api until it
     succeeds.

     If Flex creation or backend validation fails, keep the support case open
     for classic Consumption (Windows/Dynamic). Its greyed-out Total Regional
     VMs meter is not self-serviceable and may require Microsoft or the CSP
     billing partner.

     Node 22 LTS. api/package.json asks for >=20, and 20 is now past its
     end-of-life and gone from the portal's dropdown; 22 is the settled LTS on
     Functions v4. 24 works too and buys nothing here.

     FLEX/LINUX USES THE DEFAULT REMOTE BUILD. Oryx installs dependencies from
     package.json, keeping the deployment package small. Only use
     --no-remote-build if the support fallback produces a classic Windows app.

     STORAGE: LET IT CREATE A NEW ACCOUNT. Accept the default.

     The create form's storage picker sets AzureWebJobsStorage -- the Function
     App's own runtime state: host locks, function keys, timer state, logs.
     NOTHING IN THIS PROJECT READS IT. Every table, queue and blob goes
     through the AZURE_STORAGE_CONNECTION_STRING app setting instead,
     including the email-worker queue binding, and that is set in step 3.

     So the call log, the dial queues, the do-not-call list, the settings rows
     and the email-work queue all stay in the account they are in today. The
     new one holds runtime bookkeeping and nothing else.

     This also resolves a conflict the region change creates: a Function App's
     storage account must be in the SAME REGION as the app, so once quota
     pushes the app into a different region, the existing account disappears
     from the picker. It is not needed there.

     Cross-region access to the data account costs a few tens of milliseconds
     per operation and pennies of egress at this volume. Not worth relocating
     data over.

     Cloud Shell equivalent (--storage-account is the new runtime account):

       az functionapp create \\
         --resource-group  <rg> \\
         --name            <function-app-name> \\
         --storage-account <new-runtime-storage-account> \\
         --flexconsumption-location <supported-region> \\
         --runtime node --runtime-version 22

     IF CREATION FAILS WITH "Deployment validation failed":
     open Resource group -> Deployments -> the failed one -> Operation details.
     The portal's own box does not name the cause; that page does.

     Flex uses a separate regional memory quota with a documented default.
     If Flex also fails, capture the complete operation JSON. Check
     Subscription -> Overview -> Offer and billing owner; a Free Trial,
     Sponsorship, CSP restriction, Azure Policy, or tenant hold may affect
     multiple compute products. Do not infer the cause from the portal's
     one-line summary.

  2. Validate, then link it to the Static Web App -- ALSO IN CLOUD SHELL.
     Validation is non-destructive and must succeed before /api is moved.
     (Portal link equivalent: Static Web App -> APIs -> Link.)

       az staticwebapp backends validate \\
         --name <static-web-app-name> --resource-group <rg> \\
         --backend-resource-id $(az functionapp show -g <rg> \\
             -n <function-app-name> --query id -o tsv) \\
         --backend-region <function-app-region>

       az staticwebapp backends link \\
         --name <static-web-app-name> --resource-group <rg> \\
         --backend-resource-id $(az functionapp show -g <rg> \\
             -n <function-app-name> --query id -o tsv) \\
         --backend-region <function-app-region>

  3. Copy the application settings across. The Function App does NOT inherit
     the Static Web App's configuration -- this is the second most common way
     this ends up half-working, because the site loads and every write fails.

       AZURE_STORAGE_CONNECTION_STRING   <- THE EXISTING DATA ACCOUNT, not the
                                            runtime one created in step 1.
                                            This is what carries the call log,
                                            the queues, the settings rows and
                                            the email-work queue trigger.
                                            Getting this wrong points the app
                                            at an empty database that it will
                                            happily create tables in.
       ACT_SYNC / ACT_USER / ACT_PASSWORD / ACT_DB
       GRAPH_* / EMAIL_* settings used by the mail pipeline

     Portal: Function App -> Settings -> Environment variables. Or in Cloud
     Shell:
       az functionapp config appsettings set -g <rg> -n <function-app-name> \\
         --settings KEY=value KEY2=value2

     ALLOW_DEV_IDENTITY MUST NOT BE SET HERE. It exists so serve.py can run
     without a sign-in on a laptop; present in Azure it would file every rep's
     calls and every sent email under one shared pseudo-user.

  4a. Deploy -- WITH local Azure credentials (az login, or Connect-AzAccount).

     Linux:
       python src/deploy_swa.py --full --function-app <function-app-name>

     Windows (remote build is Linux-only, so node_modules is uploaded):
       python src/deploy_swa.py --full --function-app <function-app-name> \\
         --no-remote-build

     For the Windows path, api/node_modules must exist -- run `npm install`
     in api/ first. The script refuses rather than uploading an app with no
     dependencies, which deploys cleanly and then throws
     "Cannot find module" on every call.

  4b. Deploy -- WITHOUT local Azure credentials. Nothing to install.

     Cloud Shell has func and az preinstalled and already signed in, so the
     API is published from the browser and the static site from here.

     API -- only when anything under api/ changes:
       1. Cloud Shell -> Upload, and upload the api FOLDER.
          WITHOUT node_modules: the source is a few hundred KB and Cloud Shell
          sends package.json for the remote build; the folder with dependencies
          is 52 MB and the browser upload is the slow part.
       2. cd api
          func azure functionapp publish <function-app-name> --build remote

     The command above is for Flex/Linux. If Microsoft instead enables the
     classic Windows fallback, run npm install --omit=dev in Cloud Shell and
     publish without --build remote so node_modules is included.

     Static site -- every other release, from this machine:
       python src/deploy_swa.py --full --static-only

     SCM BASIC AUTH CAN STAY DISABLED for both. Azure switches it off on new
     apps by default and the warning in the portal implies deployments break;
     they do not. func and az authenticate with Entra tokens. Only the legacy
     username/password paths -- Kudu web deploy, FTP -- need it, and nothing
     here uses those. A 401 or 403 from *.scm.azurewebsites.net is the one
     symptom that would point back to this setting.

  5. Check it: sign in, open /api/health. It should answer as JSON. If it 404s,
     step 2 did not take. If it 500s, step 3 did not.

Afterwards, a webapp-only change can skip the API entirely:
       python src/deploy_swa.py --full --static-only

That is most releases. The API only needs redeploying when something under
api/ changes -- so on a machine using route 4b, the browser step is occasional
rather than part of every deploy.
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # Not `required=True`: --setup-help answers a question you have BEFORE you
    # have decided what to deploy, and demanding --test/--full to read it would
    # be a small piece of nonsense.
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--test", action="store_true",
                      help="app shell + a 200-advisor slice, to prove sign-in")
    mode.add_argument("--full", action="store_true", help="the whole dataset")
    ap.add_argument("--token", default=os.environ.get("SWA_DEPLOYMENT_TOKEN"),
                    help="deployment token (or set SWA_DEPLOYMENT_TOKEN)")
    ap.add_argument(
        "--function-app",
        default=os.environ.get("AZURE_FUNCTION_APP_NAME"),
        help=("linked Azure Function App name (or set "
              "AZURE_FUNCTION_APP_NAME)"),
    )
    ap.add_argument(
        "--static-only",
        action="store_true",
        help=("upload only webapp/; use only when the linked Function App is "
              "already deployed"),
    )
    ap.add_argument(
        "--no-remote-build",
        action="store_true",
        help=("upload api/node_modules instead of letting the server install "
              "from package.json; needed for a Windows Function App"),
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="stage and report, but do not upload")
    ap.add_argument("--setup-help", action="store_true",
                    help="print the one-off Azure setup for a linked Function App")
    args = ap.parse_args()

    if args.setup_help:
        print(SETUP_HELP)
        return
    if not (args.test or args.full):
        ap.error("one of --test or --full is required (or --setup-help)")

    if args.function_app and args.static_only:
        ap.error("--function-app and --static-only cannot be used together")
    if args.no_remote_build and not args.function_app:
        ap.error("--no-remote-build only applies with --function-app")

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

    if linked_required and not args.function_app and not args.static_only:
        # NOT A DEPLOYMENT PROBLEM TO WORK AROUND -- a managed Static Web Apps
        # API runs HTTP triggers and nothing else, so a queue-triggered worker
        # uploaded there would simply never run. The pipeline would look
        # deployed, drafts would sit in "pending" forever, and the cause would
        # be invisible. Hence a refusal rather than a warning.
        sys.exit(
            "[!] api/ contains a non-HTTP trigger and cannot be uploaded as a "
            # Plain hyphen, not an em dash: this prints to a Windows console
            # whose codepage renders one as a replacement character, and a
            # mojibake byte in an error message reads as a second fault.
            "managed Static Web Apps API - it would deploy and never run.\n"
            f"    non-HTTP: {', '.join(queue_functions) or 'see above'}\n"
            "    Fix: host the API on a linked Function App.\n"
            "      1. one-off setup, if not done:  "
            "python src/deploy_swa.py --setup-help\n"
            "      2. then deploy:                 "
            "python src/deploy_swa.py --full --function-app <name>\n"
            "    Or, when that linked backend is already current, upload\n"
            "    webapp/ alone:                  "
            "python src/deploy_swa.py --full --static-only"
        )

    folder = stage("test" if args.test else "full")

    if args.dry_run:
        if args.function_app:
            print(f"[*] dry run -- would publish Function App {args.function_app!r}")
        elif args.static_only:
            print("[*] dry run -- API deployment intentionally skipped")
        print(f"[*] dry run -- staged at {folder}")
        return
    if not args.token:
        sys.exit("[!] no deployment token. Pass --token or set "
                 "SWA_DEPLOYMENT_TOKEN. Find it in the portal under the "
                 "Static Web App -> Overview -> Manage deployment token.")

    cmd = ["swa", "deploy", str(folder), "--deployment-token", args.token,
           "--env", "production"]
    if args.function_app:
        publish_function_app(args.function_app,
                             remote_build=not args.no_remote_build)

    if bindings and not linked_required and not args.static_only:
        cmd += ["--api-location", str(API)]
        print(f"[*] including managed HTTP API from {API}")
    elif not bindings:
        print("[!] no api/host.json -- deploying WITHOUT the call log endpoints. "
              "The dialer will report that logging is unavailable.")
    else:
        print("[*] Static Web App upload excludes api/; /api is served by the "
              "linked Function App")
    print("[*] swa deploy " + str(folder) + " --env production")
    try:
        run_checked(cmd)
    except FileNotFoundError:
        sys.exit("[!] the SWA CLI is not installed. Run:\n"
                 "    npm install -g @azure/static-web-apps-cli")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"[!] Static Web App deployment failed (exit {exc.returncode})")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    main()
