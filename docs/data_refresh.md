# Advisor Map data refresh runbook

Use `src/refresh_data.py` to acquire or import sources, build an isolated candidate,
review changes, and prepare a validated release. A rebuild or code deployment does
not obtain fresh source data. None of these commands sends email, changes ACT!,
uploads data, deploys the app, or enables a recurring schedule.

## Start with a plan

Run commands from the repository in PowerShell. The existing Python dependencies
and local API npm dependencies must be installed. Keep the network share available.
Use a protected local staging parent with adequate disk space for raw snapshots,
baseline artifacts, regenerated data and private release archives.

```powershell
python src/refresh_data.py plan --mode act-only
python src/refresh_data.py plan --mode selected-firm --firm wells_fargo --firm ubs
python src/refresh_data.py plan --mode sec-only
python src/refresh_data.py plan --mode rankings-only
python src/refresh_data.py plan --mode full
```

Plans inspect local sources and show the dependency graph. They do not create a
run or refresh anything. `--dry-run` on a mutating action also performs no writes.
Selected-firm names come from `src/firm_rosters.py`.

Rankings-only reuses a validated map/contact/recipient baseline and rebuilds ranking
matches, exports and Field ranking flags. It does not rerun ACT matching or geography.
A Barron's update also rematches available Forbes data because Barron's is matching
evidence. Missing baseline dependencies require a full build, not a partial release.
ACT-only and selected-firm runs still match globally: a changed email or roster can
affect uniqueness and identity evidence beyond one firm.

## Create and operate a run

```powershell
python src/refresh_data.py start --mode act-only
python src/refresh_data.py status --run RUN_ID
python src/refresh_data.py build --run RUN_ID
python src/refresh_data.py report --run RUN_ID
python src/refresh_data.py validate --run RUN_ID
python src/refresh_data.py prepare-release --run RUN_ID
python src/refresh_data.py verify-release --run RUN_ID
```

Replace RUN_ID with the ID returned by start. Start copies code, immutable inputs,
historical ACT preferences and baseline data into a marked local workspace. It
records the original checkout commit, dirty state, code hashes and source hashes.
The checkout's application data and current API lookups are not replaced.

A bootstrap manifest is saved before source inventory or hashing. Status shows
`capture` or `copy` while staging. If interrupted, use the same `resume --run RUN_ID`:
partial files are checksum-verified or recopied atomically. Once the snapshot plan
is pinned, changed originals require a new run. An interruption before any plan was
registered may reselect current inputs, with an explicit warning; no build outputs
have been used at that point. Staging recovery never recopies over a started build.

Targeted modes pin reused normalized SEC evidence rather than copying the raw firm
API/ownership caches needed only by SEC/full reduction. Legacy normalized data with
unknown original retrieval dates stays explicitly unknown; an unrelated raw archive
on disk is not described as its source. Source inventory and candidate-used inventory
are retained separately in the manifest.

Private run records live under `data/refresh/runs/RUN_ID`. `run.json` identifies the
staging workspace; logs, reports and candidate archives stay in this ignored area.
Treat both staging and these records as confidential. Do not upload them to Pages,
public file sharing, source control or an unauthenticated storage container.

`status` is the lifecycle stage; `outcome` distinguishes `success`,
`success_with_warnings`, `incomplete` and `failed`. Review the warnings array and
source attempt results before approving a release, even when the stage is `built`.

Start does not acquire sources. With no import or acquisition, build is an explicit
rebuild from existing registered snapshots. This is useful for a controlled rehearsal
and does not make old data fresh.

## Acquire or import sources

Acquisition is a separate action requiring `--allow-network`. Credentials belong in
the current user's environment or the existing approved credential mechanism, never
in source files or command arguments. Builds and tests scrub credential variables
and block external Python and Node network connections.

```powershell
python src/refresh_data.py acquire --run RUN_ID --source act --allow-network --act-user USER --act-db DATABASE
python src/refresh_data.py acquire --run RUN_ID --source roster:wells_fargo --allow-network
python src/refresh_data.py acquire --run RUN_ID --source sec_individuals --allow-network
python src/refresh_data.py acquire --run RUN_ID --source sec_firms --allow-network
```

ACT acquisition uses the existing paired contact/owner pull and requires ACT_PASSWORD.
It does not write ACT. Firm acquisition retains the existing scraper pacing and
checkpoints. Browser-dependent scrapers require the appropriate Chrome debug session.
Morgan Stanley runs advisor directory, office discovery and team supplement stages;
directory-only output is not a complete refresh.

Acquisition runs in a separate scratch workspace. Failed or partial downloads do
not replace the run's last good snapshot. Failed attempts remain visible, even
when a later build intentionally reuses the prior successful snapshot.

Import is the supported path for sources already obtained:

```powershell
python src/refresh_data.py import --run RUN_ID --source act --file PATH_TO_PAIRED_ACT_MANIFEST
python src/refresh_data.py import --run RUN_ID --source roster:ubs --file PATH_TO_CSV --source-date YYYY-MM-DD
python src/refresh_data.py import --run RUN_ID --source sec_individuals --file PATH_TO_SEC_ZIP
python src/refresh_data.py import --run RUN_ID --source sec_firms --file PATH_TO_SEC_GZIP
python src/refresh_data.py import --run RUN_ID --source sec_adv --file PATH_TO_ADV_CSV_OR_ZIP --source-date YYYY-MM-DD
python src/refresh_data.py import --run RUN_ID --source barrons --file PATH_TO_JSON --edition YYYY --category top100
python src/refresh_data.py import --run RUN_ID --source forbes --file PATH_TO_JSON --edition YYYY --category best-in-state
```

Importing a file is not evidence of when it was retrieved. Unknown retrieval dates
remain unknown. Existing immutable filenames cannot be silently replaced with
different bytes. A deliberately selected older SEC input displaces newer selectors
only inside staging, with their evidence retained in the source archive.

Run modes constrain imports: ACT-only accepts ACT; selected-firm accepts only the
declared firms; SEC-only accepts SEC inputs; rankings-only accepts rankings; full
accepts all supported source types. Create another run to change the scope.

The firm ADV download is separate from SEC individual/firm XML. Obtain the official
CSV/ZIP and import it explicitly. The old developer Downloads default is retired.
SEC firm compilation currently means the SEC-registered firm feed used by the
application; this workflow does not silently expand the map to state-only RIAs.
All three SEC imports compare native CRDs and field continuity with the selected
baseline; ADV also checks numeric usability and reported asset changes. Suspicious
changes require exact-snapshot review. Malformed archives, unsupported schemas and
ambiguous identifiers cannot be approved through an override. See the source-specific
policies and tested repair workflow in [Acquisition resilience](acquisition_resilience.md).

Roster imports check schema, named records, email, phone, contactability and total
coverage, plus provider-aware historical identity, field and geographic continuity.
Suspicious changes require an exact-snapshot `review-acquisition` decision; a generic
`--reduction-reason` cannot override the historical gate. Missing people are not
automatically confirmed departures. See [Acquisition resilience](acquisition_resilience.md)
for source-specific limits, review commands and supervised repair boundaries.
Morgan Stanley imports require a CSV-bound `.completeness.json` sidecar confirming
office and team completeness; inspect the crawler coverage before asserting it.

## Harvest and preserve rankings

Barron's requires the existing signed-in browser workflow. Forbes uses its existing
browser harvest. Follow the instructions at the top of each harvest script.
Do not bypass authentication. Configure editions and categories before running:

```javascript
BARRONS.configure({years:{top1500:2027}, categories:["top1500"], states:["ga"]})
FORBES.configure({years:{"best-in-state":2027}, categories:["best-in-state"]})
```

These years are examples, not claims that those editions are published. Use the
actual edition shown by the publisher. Import mixed-year Barron's categories
separately or retain their explicit row years rather than relabeling them.

Immutable edition files are merged without deleting historical recognition.
Someone absent from a new edition may remain a historically ranked prospect.
The app shows the latest known recognition year. Unknown legacy Forbes years stay
unknown. Historical team-asset observations retain their year; they are not summed
or presented as newly obtained current assets.

## Understand build order

SEC/full runs parse the pinned feeds, score firms and rebuild normalized firm
reference data from pinned caches. Cached API aliases and ownership are separate
sources: reducing their caches is not a new external retrieval.

Common builds regenerate the identity ledger, ACT economic links and contact
details before rebuilding working locations and map membership. Asset publication
uses the new staged advisor index, not the previous release's index. ACT lookup
generation precedes opt-out generation, preserving deleted-contact opt-outs.
Recipient shards and the API descriptor come from the same identity/contact build.

State membership is recomputed from current branch records. Exact-address cached
coordinates are reused; a moved advisor cannot inherit coordinates merely because
the CRD is unchanged. External geocoding is not triggered by a routine build.
Review unresolved addresses and obtain separate approval before additional free
service calls or a bounded paid allowance.

## Review and validate

`reports/comparison.json` summarizes coverage, source reuse, asset reconciliation
and changes. Detailed private CSVs cover contact information, identity/economic
links, employment, working offices, placements, territory, ranking matches and
recipient eligibility. Removed means absent from an artifact, not confirmed retired.

Validation checks map/search consistency, working locations, assets, the audit,
Python tests, API tests and compressed/cache-versioned assets. Missing artifacts
or failed checks cannot become an approved candidate.

A large coverage loss requires review and an explicit `--coverage-reason` on
validate. Do not use this override to excuse broken parsing or a partial scrape.
`--skip-tests` is diagnostic only; production verification rejects that evidence.

## Resume and diagnose

```powershell
python src/refresh_data.py status --run RUN_ID
python src/refresh_data.py resume --run RUN_ID --through build
python src/refresh_data.py resume --run RUN_ID --through prepare-release
```

Inspect the named step log. A failed step stops its dependents and is not reported
as complete. Operating-system locks prevent two operations from mutating one run.
Process termination releases the lock; do not delete lock files to force a restart.

Successful work is reused only while its code, inputs and outputs still match.
Source or controller edits invalidate that guarantee. Create a new run rather than
editing a staged workspace or transplanting a success marker. Changing a source
through the supported import/acquire action records the change and invalidates
affected build/release evidence.

## Prepare and publish a consistent release

Preparation produces `candidate/api.tgz`, `candidate/static.tgz`, required private
lookups and `candidate/release.json` with checksums. The existing API archive verifier
also inspects the package. A prepared candidate is not a production publication.

```powershell
python src/refresh_data.py verify-release --run RUN_ID --production
```

Production verification requires committed source and the full test evidence.
An implementation rehearsal from a dirty worktree remains an explicitly dirty
candidate. After code approval/commit, create a clean run; do not relabel its bytes.

Obtain separate approval before pushing code, uploading private data or deploying.
Verify the candidate immediately before transfer. Upload its immutable recipient
shards first, then their matching manifest and required private lookups; publish
the matching static/API artifacts in the coordinated release procedure.
See `deployment_automation.md` for authentication and fresh-directory Function
publishing. Do not rebuild from a changing checkout between approval and upload.

### Deploy the exact prepared candidate

The commands in this section are live operations requiring the separate approvals
above. Do not run the ordinary exporter `--upload` commands or API build command:
those regenerate checkout outputs rather than transfer the approved candidate.
Use a quiet, coordinated deployment window and check active or scheduled batches
before changing the application. Keep the prior verified release available.

From the clean operational checkout that prepared the approved run:

```powershell
$candidateRun = 'RUN_ID'
python src/refresh_data.py verify-release --run $candidateRun --production
if ($LASTEXITCODE -ne 0) { throw 'Candidate verification failed; stop.' }
$candidateDir = (Resolve-Path "data/refresh/runs/$candidateRun/candidate").ProviderPath
```

Confirm the storage credential targets account `eicadvisorlog`, private container
`lookups`. The following uses the existing upload functions with the supplied
candidate files; it does not call their rebuilding command-line entry points.
It requires the Azure Blob SDK and an approved process-scoped
`AZURE_STORAGE_CONNECTION_STRING`. Never print or commit that credential.

```powershell
$candidateUpload = @'
import json, sys
from pathlib import Path
sys.path.insert(0, "src")
from export_approved_recipients import upload_shards
from export_advisor_emails import upload
candidate = Path(sys.argv[1])
manifests = list((candidate / "lookups/approved_recipients/releases").glob("*/manifest.json"))
if len(manifests) != 1:
    raise SystemExit("Expected exactly one candidate recipient manifest")
manifest = manifests[0]
payload = json.loads(manifest.read_text(encoding="utf-8"))
shards = [candidate / "lookups" / entry["blob"]
          for entry in payload["shards"].values()]
status = upload_shards(manifest, shards)
if status:
    raise SystemExit(status)
raise SystemExit(upload(candidate / "lookups/advisor_emails.json.gz"))
'@
python -c $candidateUpload $candidateDir
if ($LASTEXITCODE -ne 0) { throw 'Private dependency upload failed; do not deploy.' }
```

`upload_shards` uploads every supplied shard before its manifest. Confirm the
uploaded release prefix, shard count and manifest hash match the candidate.
Keep older release prefixes; do not overwrite them with different content.
The activity email lookup uses a stable blob name, so retain its prior candidate
copy for rollback. Check all upload results before publishing either application.

For the Function App, upload the exact `candidate/api.tgz` to Cloud Shell under
the distinct filename used below. Read its expected SHA-256 from the
`files["api.tgz"].sha256` entry in `candidate/release.json`. Replace both
placeholders before running:

```bash
set -euo pipefail
candidate_api="$PWD/advisor-refresh-RUN_ID-api.tgz"
test "$(sha256sum < "$candidate_api" | awk '{print $1}')" = 'EXPECTED_API_SHA256_FROM_RELEASE_JSON' || exit 1
candidate_api_dir=$(mktemp -d /tmp/advisor-candidate-api.XXXXXX)
tar -xzf "$candidate_api" -C "$candidate_api_dir"
cd "$candidate_api_dir"
func azure functionapp publish eic-advisors-api --build remote
```

Use a fresh extraction directory, not an existing `~/api`. Verify the API release
and storage health through the authenticated Static Web Apps origin before the
static switch. Do not substitute the checkout's `dist/api.tgz`.

For the static app, extract the verified candidate archive into a fresh local
directory. Authenticate using the SWA CLI keychain or the environment-only token
procedure in `deployment_automation.md`; no token belongs in command arguments.
Replace `APPROVED_STATIC_APP_NAME` with the operator-verified Azure resource name:

```powershell
$candidateStaticDir = Join-Path ([IO.Path]::GetTempPath()) ('advisor-candidate-static-' + [guid]::NewGuid().ToString('N'))
$approvedStaticAppName = 'APPROVED_STATIC_APP_NAME'
if ($approvedStaticAppName -eq 'APPROVED_STATIC_APP_NAME') { throw 'Set the verified Azure static app name first.' }
New-Item -ItemType Directory -Path $candidateStaticDir | Out-Null
tar -xzf (Join-Path $candidateDir 'static.tgz') -C $candidateStaticDir
if ($LASTEXITCODE -ne 0) { throw 'Static extraction failed; stop.' }
swa deploy $candidateStaticDir --env production --app-name $approvedStaticAppName
if ($LASTEXITCODE -ne 0) { throw 'Static deployment failed; investigate before proceeding.' }
```

This deploys the extracted approved bytes, not checkout `webapp`. Complete the
runtime checks and receipt procedure below. A partially completed deployment is
not a successful release; either finish the matching pair after investigation
or restore the previous verified static, API and private dependencies.

After publication, verify through the authenticated app: static fingerprint,
recipient registry hash, API commit, storage health and source snapshot identities.
Only that evidence supports a publication receipt. The admin freshness endpoint
accepts a receipt bound to those exact values; without it, published provenance
is unavailable, not inferred from a recent deployment timestamp.

Do not edit the frozen candidate descriptor to mark it published. After a
separately approved deployment, verify the live app's `DATA_VERSION`, authenticated
`/api/health` release commit/clean state and storage health, and the uploaded
recipient manifest/shard checksums. Compare the live freshness candidate and source
snapshot identities with the release. Then prepare the operator-attested receipt:

```powershell
python src/refresh_data.py prepare-publication-receipt --run RUN_ID --published-utc ACTUAL_PUBLICATION_UTC --api-commit OBSERVED_API_COMMIT --static-fingerprint OBSERVED_DATA_VERSION --recipient-registry-hash VERIFIED_REGISTRY_HASH --confirm-runtime-verified
```

This requires a production-verifiable clean candidate and matching observed values.
It writes only `data/refresh/runs/RUN_ID/publication/receipt.json`, outside the frozen
candidate. It does not perform those live checks, change Azure, or authorize a deploy.
An administrator can paste that file's JSON into the Function App setting
`DATA_FRESHNESS_PUBLICATION_RECEIPT_JSON` after the observations have been verified.
The read-only API overlays this backend receipt without changing the package and
rejects malformed or mismatched evidence. Never populate the setting merely because
local preparation succeeded. On rollback, restore the matching verified receipt or
remove the setting; an old mismatched receipt must not mark the new release published.

Keep previous verified code and data bundles. Rollback needs the matching static,
API and lookup dependencies. It does not reverse ACT edits, sent email, or other
external data changes.

For an approved rollback, verify the retained package and lookup hashes against
the previously approved release manifest and its pinned run record, restore that
release's private dependencies, and deploy its exact API/static archives using
the same transfer steps. Restore the prior stable-name activity lookup too.
Use the API archive verifier with the recorded archive SHA-256. If the controller
checkout has since changed, the current full-run verifier may reject the old
controller identity; do not edit old manifests to bypass that check or rebuild
old code into replacement data. Use the retained approved archival hashes and
record the explicit rollback approval and runtime checks. Restore the matching
publication receipt or clear it as described above.

### Retain the published baseline before the next refresh

For live runs, choose a durable protected `--stage-parent` with adequate free
space; do not rely on operating-system temporary-folder retention. For example:

```powershell
python src/refresh_data.py start --mode act-only --stage-parent '\\EIC-FS09\DATA\_Research\Power_BI\adv_targeting\data\refresh\workspaces'
```

Retain the complete verified run directory and its workspace in place, together
with candidate archives, comparison reports, publication receipt and committed
code identity. The workspace contains raw snapshots, immutable source history,
receipts and normalized SEC caches that the deployable archives do not contain.
Keep restricted backups of these locations and the previous published baseline.
Moving a workspace changes recorded paths; do not edit manifests or transplant
success markers to make a moved or modified workspace appear verified.

Publication does not automatically adopt staged sources or generated data into
the operational checkout. An ordinary new `start` reads that checkout, not the
last published run. Without reconciliation it could select older inputs and
compare against an older generated baseline.

Before starting the next live refresh, an operator must complete a separately
reviewed, path-scoped adoption checkpoint:

1. Verify and retain the just-published run and a backup of the previous
   operational data. Keep application source and `.git` under normal code
   control; never copy an entire staged workspace over the checkout.
2. Reconcile selected source snapshots, immutable ranking/source history,
   source receipts, identity decisions, ACT preference floor, geocoding caches,
   normalized inputs and generated baseline with that published run. Restore
   only the intended generated API lookups, not staged `local.settings.json`,
   credentials, runtime settings or unrelated files.
3. Check the next read-only plan's selected ACT pair and firm/SEC/ranking
   snapshot hashes against the published inventory. After staging and before
   building, separately compare the new run's `baselineArtifacts` with the
   retained published run's generated artifact facts and private lookups;
   `plan` does not report baseline hashes. Resolve every mismatch or record a
   deliberate different comparison baseline before building.

Alternatively, explicitly import the retained selected snapshots into a new run
and document why its checkout comparison baseline differs from production.
Imports alone do not update that baseline. Do not describe the resulting
comparison as a production before/after comparison unless its baseline agrees.
No automatic promotion, recurring refresh or production mutation is enabled.

## Administrator freshness and operating cadence

Existing administrators can open Settings → Data administration → Data freshness.
This read-only view separates source dates, retrieval attempts, candidate build
and verified publication. Legacy unknown dates are not inferred from file mtime.
No salesperson permissions, sending caps or mailbox access are changed.
The Field app retains a compact sales workflow rather than an admin dashboard.

`refresh_policy.json` configures freshness expectations and the coverage gate.
Suggested operation is weekly ACT updates, monthly major rosters and SEC
employment refreshes, quarterly firm ADV updates, and ranking updates when each
edition is published. These are expectations, not enabled scheduled jobs.

The first production refresh should follow a successful local ACT-only rehearsal
and reviewed comparison. Any blocked source needs its specific credential, browser
session, complete input or approval; unrelated work can proceed independently.
