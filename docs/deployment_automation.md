# Manual deployment with verifiable artifacts

The web dataset and two generated Act! lookup files are intentionally absent
from Git. Static Web Apps and the standalone Function App therefore have
separate release paths. Neither path stores a credential in this repository.

## Values and prerequisites

Replace values in angle brackets in the commands below:

- <FUNCTION_APP>: currently eic-advisors-api
- <STATIC_WEB_APP_URL>: the authenticated Static Web Apps origin

Use Node 22.12 or newer locally, and configure the Azure Function App for the
Node 22 runtime. Allowing a newer local Node (currently Node 26) is deliberate
for workstation testing; production stays on Azure's supported Node 22
Functions runtime. Install Azure Functions Core Tools v4 and the SWA CLI.
Cloud Shell already supplies Core Tools and Azure authentication.

For an instrumentation-only static release and the required post-deploy desktop
benchmark sequence, see [performance_profiling.md](performance_profiling.md).

## Use the saved SWA token

Do not pass --token. The current setup stores the documented
SWA_CLI_DEPLOYMENT_TOKEN as a user-scoped Windows environment variable. Load it
into this process, verify that it exists, deploy, and remove the process copy:

    $deployToken = [Environment]::GetEnvironmentVariable('SWA_CLI_DEPLOYMENT_TOKEN', 'User')
    if ([string]::IsNullOrWhiteSpace($deployToken)) {
      throw 'SWA_CLI_DEPLOYMENT_TOKEN is not saved for this Windows user.'
    }
    try {
      $env:SWA_CLI_DEPLOYMENT_TOKEN = $deployToken
      python src/deploy_swa.py --full --static-only
    } finally {
      Remove-Item Env:SWA_CLI_DEPLOYMENT_TOKEN -ErrorAction SilentlyContinue
      $deployToken = $null
    }

This avoids shell history and process arguments, but a User environment
variable is plaintext in HKCU and readable by other processes running as the
same Windows user. It is accepted here as a short-lived convenience, not a
keychain. Remove the saved copy with:

    [Environment]::SetEnvironmentVariable('SWA_CLI_DEPLOYMENT_TOKEN', $null, 'User')

Then rotate the deployment token in the Azure Static Web App portal. Saving a
new token replaces the old user value; already-running shells must reload it.

## Build the API

Commit the intended source first. The packager refuses tracked changes and
untracked, non-ignored source because an archive claiming an older commit is
not auditable. Git-ignored generated ACT files remain allowed and are validated
separately.

    npm ci --prefix api
    npm test --prefix api
    & 'C:\Program Files\Git\bin\bash.exe' src/build_api.sh

The script repeats the tests and validates that act_contacts.json and
act_mail_codes.json are present, parseable, populated mappings. It produces:

- dist/api-<commit>-<UTC>.tgz, the immutable release artifact
- its matching .sha256 file
- fresh dist/api.tgz and dist/api.tgz.sha256 convenience copies

It uses a new temporary staging directory, excludes tests, dependencies, tools,
logs and local settings, then adds a non-secret Functions runtime stub and
whitelisted release.json provenance. The two Act! files contain internal
identifiers, so every API archive is confidential.

Before copying anything, the packager rejects source symlinks and obvious
secret, key, certificate, backup, and nested-archive filenames, including
.env files, local.settings.json, private-key formats, .bak, .zip, .tar, and
.tgz. Verification repeats these checks against the staged archive. The only
local.settings.json allowed is the exact empty runtime stub generated inside
the temporary staging directory.

ALLOW_DIRTY_BUILD=1 exists only for an explicitly dirty test package. Its
health response says dirty: true; never deploy one to production.

## Publish through Cloud Shell

Upload dist/api.tgz and dist/api.tgz.sha256 using the Cloud Shell upload
control. Then run:

    set -euo pipefail
    cd ~
    sha256sum -c api.tgz.sha256

    release_dir=$(mktemp -d "${TMPDIR:-/tmp}/advisor-api.XXXXXX")
    cleanup() {
      case "$release_dir" in
        "${TMPDIR:-/tmp}"/advisor-api.*) rm -rf -- "$release_dir" ;;
        *) echo "Refusing unsafe cleanup path: $release_dir" >&2 ;;
      esac
    }
    trap cleanup EXIT

    tar -tzf ~/api.tgz | while IFS= read -r member; do
      case "/$member/" in *"/../"*) echo "Unsafe archive path: $member" >&2; exit 1 ;; esac
      case "$member" in /*|*\\*) echo "Unsafe archive path: $member" >&2; exit 1 ;; esac
    done
    tar -xzf ~/api.tgz -C "$release_dir"
    cd "$release_dir"
    func azure functionapp publish <FUNCTION_APP> --build remote

A fresh directory is essential. The former mkdir -p ~/api procedure could
retain a deleted or renamed function. Flex Consumption uses OneDeploy;
--build remote lets Azure restore the locked npm dependencies for Linux. The
SWA token is unrelated and cannot authorize a Function App deployment.

## Verify and release

In the already authenticated Chrome session, open:

    <STATIC_WEB_APP_URL>/api/health

Confirm storageOk is true, Act! has the expected state, release.available is
true, release.commit matches the packaged commit, and release.dirty is false.
Use the Static Web Apps origin, not the direct Function App hostname, because
the SWA edge supplies the authenticated principal.

If a web release depends on the API contract, verify the API first and then
run the token-backed --static-only deployment.

## Roll back

Keep several immutable archives and hashes in protected storage. Verify the
selected prior pair locally:

    & 'C:\Program Files\Git\bin\bash.exe' src/build_api.sh --verify dist/api-<prior>.tgz dist/api-<prior>.tgz.sha256

Upload that exact archive and hash and repeat the fresh-directory Cloud Shell
publish. Do not rebuild an old commit during an incident. Code rollback cannot
undo Azure Table data, so storage changes must remain backward compatible.
