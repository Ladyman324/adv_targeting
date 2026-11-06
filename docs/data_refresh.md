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

## One-command supervised workflow

The `run` command creates (or resumes) an isolated run, acquires the selected
sources, builds the candidate and runs validation. It never approves held source
changes, calls an AI repair session, uploads, deploys, sends email or writes ACT.
Start with a small scope:

```powershell
python src/refresh_data.py run --run wells-trial-20261025 --mode selected-firm --firm wells_fargo --source roster:wells_fargo --through acquire --allow-network --dry-run
python src/refresh_data.py run --run wells-trial-20261025 --mode selected-firm --firm wells_fargo --source roster:wells_fargo --through acquire --allow-network
python src/refresh_data.py run --run wells-trial-20261025 --allow-network
```

Use a durable protected local `--stage-parent` on the first invocation to reduce
network-share copying and avoid temporary-folder expiry. The last command resumes
the saved source plan and continues through build and validation. Add
`--through prepare-release` to prepare archives for a separately approved release.
Completed source acquisitions are not repeated. If code or pinned inputs changed,
start a new run rather than editing its saved evidence. Never run two operations
against the same run concurrently.

Read `data/refresh/runs/RUN_ID/workflow/summary.json` and `source_review.csv` for
the combined source outcomes. Exit code 2 means sources need attention, not that
the refresh succeeded. Transport failures, historical-review holds and deliberate
last-good reuse are distinguished. Interrupted work resumes; use `--retry-failed`
to retry failed or held acquisitions after resolving their cause.

An unavailable source blocks the build by default. To deliberately retain its
existing snapshot, name each source and record why:

```powershell
python src/refresh_data.py run --run wells-trial-20261025 --allow-network --reuse-last-good roster:wells_fargo --fallback-reason 'Reviewed publisher outage; retain the existing Wells Fargo snapshot'
```

Reuse is only allowed when that source has an existing pinned snapshot. The report
preserves its original date and retrieval evidence; it is not labeled fresh.
No blanket fallback approval covers other sources. A held new snapshot remains
held even if its old snapshot is reused. Review/import tools below remain the
authority for accepting exact held data.

`--mode full` plans all sources, but does not make every adapter unattended.
SEC ADV downloads, browser ranking captures and Raymond James branch captures
still require operator handoff/import. Missing sessions, blocked publishers and
changed layouts may also need intervention. Do not schedule unattended production
refreshes until these paths and two complete supervised runs are verified.

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

### Minor discrepancies and publication holds

Minor source discrepancies are published with warnings and a private review report.
ACT account copies are deduplicated by broker account code using the highest
recorded total among advisors included in the view. Product allocations stay with
that same source copy; product maxima are never combined. Individual advisor
cards and single-advisor filters retain that advisor's own ACT amounts.
`data/identity/act_account_value_review.csv` lists every differing copy, timestamps
and the summary selection. The economic manifest binds its hash. Run warnings,
release comparisons and scheduled summaries surface the issue without requiring
an ACT edit. This policy applies to weekly and quarterly refreshes; refreshes never
write corrections back to ACT.

Major issues retain their existing holds: malformed or changed source evidence,
ambiguous native identifiers, suspicious missing cohorts, unsafe identity/routing
links, inconsistent artifacts and failed release checks. A minor account-value
warning does not waive these gates or make an unapproved advisor publishable.

If implementation changes after acquisition, start a fresh run with
`start --mode full --source-run PRIOR_RUN --run-id NEW_RUN` and the normal
`--stage-parent`. This verifies the prior run is idle, ownership-marked and still
matches its pinned input hashes. Current code and the original checkout baseline
are captured separately from those saved inputs. Prior source holds remain warnings;
no prior build, validation or release success is reused. Every output is rebuilt.
The prior manifest and sources must stay unchanged until staging completes.

A clean isolated checkout can add `--source-run-root SOURCE_CHECKOUT` to name the
checkout that owns `PRIOR_RUN`. The saved manifest, workspace owner, scope and
input hashes are verified at that location; the new checkout supplies current
code and its separately reviewed comparison baseline. No prior build/test pass
is transferred. This avoids committing unrelated work to publish an app update.

An explicitly reviewed SEC refresh may carry a private
`data/identity/roster_identity_holds.json` input. Each hold binds a published
roster name/email and prior CRD to the exact roster filename and SHA-256. A held
record cannot attach contact details to a different CRD. Duplicate roster
witnesses cannot route the same disputed email around the hold. Existing prior
owners and CRM identity decisions retain their existing rules; holds never grant
an identity, permission or email route. The anchor pass cannot promote held rows.

The contacts build writes `data/identity/roster_identity_review.csv` and binds it
as a declared output. Private comparisons and run warnings surface the withheld
assignments without holding up unrelated records. Changed/malformed review
inputs still fail validation. Keep the hold input and reports outside Git; use a
fresh source-pinned run after changing a review decision. A missing SEC record
is an unresolved match, not a verified retirement or departure.


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
Approved operation is ACT every Saturday at 03:00 Eastern, with all other
external sources every three months on the first Saturday of January, April,
July and October at 03:00 Eastern. The quarterly run includes ACT, so one
dispatcher avoids a duplicate weekly pull. Ranking recognition years retain
their original edition; the retrieval cadence does not relabel old recognition.

The first production refresh should follow a successful local ACT-only rehearsal
and reviewed comparison. Any blocked source needs its specific credential, browser
session, complete input or approval; unrelated work can proceed independently.

### Public-browser roster capture

Red Door's redesigned directory is captured by `src/red_door_browser.py`,
selected automatically by the acquisition registry. Use an isolated trial:

```powershell
python src/refresh_roster_trial.py --trial red-door-UNIQUE --firm red_door --allow-network
```

Chrome's existing debug endpoint must be available on `127.0.0.1:9222`.
The collector creates and closes only its own context. It follows all published
directory pagination and person links, records profile observations privately,
and refuses partial output or ambiguous contact claims. Access denials stop
without challenge handling, cookie export, or automatic retries.

The redesigned source currently omits profile email addresses and phone numbers.
Those blanks must remain blanks in the current source snapshot; do not fabricate
addresses or copy old values into a file labeled freshly acquired. The historical
comparison intentionally holds this candidate for contact-loss review. Deciding
whether to retain historical contact fields requires explicit provenance and
downstream identity review, not merely approving the download.

`src/ameriprise_browser.py` uses the publisher's public locator endpoint in its
normal browser context. The city-page Show more control did not reliably finish;
the public locator canary verified all 81 Atlanta results across two pages.
The adapter follows the published state/city index and advances by the endpoint's
fixed 50-record page offsets, even when a page returns fewer records. Every city must satisfy its
advertised count with distinct native IDs. No extracted tokens, cookie export,
or challenge handling are used; a denial stops immediately.

Per-city JSONL checkpoints retain completed queries on failure. A new isolated
trial can explicitly seed a retained Ameriprise checkpoint via `--checkpoint`.
Changed collector code or a prior access denial requires review rather than
automatic resume. A successful city canary is not national completion: all
published states and cities still need a complete capture and source review.

Ameriprise city-level coverage inconsistencies are retained with their raw
public records in the private JSONL journal. Other independent cities continue;
an access denial still stops the entire host immediately. Any unresolved city
blocks canonical roster output. A reviewed checkpoint migration may reuse only
previously complete queries when a collector correction changes its code hash;
the migration must retain original source hashes and capture timestamps.

UBS uses repeated random locator draws until saturation, not an authoritative
census count. Only valid nonempty entity draws can count toward saturation;
transport failures, denials, malformed replies and an unsaturated call ceiling
stop the run. Locator-only trials cannot replace the full roster. Team-page
attempts have a private JSONL observation journal and completeness receipt;
unresolved pages block output and queued requests are canceled on failure.
`--dry-run` retains its no-write behavior. These guards must not be retroactively
claimed for an older capture made by code that silently returned empty results.

`alex_brown_modern.parse_modern_team` handles the current published team-card
layout separately from legacy branch pages. It requires card-scoped contacts
and HTTPS publisher/practice-scoped biography links. A single corroborated
office supplies branch geography. Multiple, missing, or conflicting offices
leave geography blank while preserving valid own-card names, emails, and phones;
whole published office tuples remain in source-review metadata.
Migrated-branch preview files are not operational rosters. A state/branch
disappearance must be investigated even when the overall person count grows.

`alex_brown_modern.parse_yearbook_team` separately handles the publisher's
yearbook-card layout. Names, biography links, emails and phones stay within one
card. Exactly one footer office must agree with the page's city/state title
before it can supply geography; otherwise valid contact cards remain unlocated.
Multiple personal phone claims are retained rather than replaced by a generic
switchboard. Replaying retained
HTML after a parser fix creates a separate review receipt, never rewrites the
original acquisition result or timestamp.

The standard alex_brown_async.py refresh now includes the two source-verified
modern team URLs and official team URLs from the prior roster, and dispatches
both current layouts. It retains source bytes/encoding and a JSONL observation
journal outside operational inputs. Migrated branches, unavailable pages, or
unrecognized person markup hold the candidate in data/interim/firm_rosters;
they cannot silently replace the canonical roster. Access denials stop the run
without retry or following Raymond James destinations. A successful complete
write is atomic. The --dry-run option creates neither a roster nor private
evidence files.

### Offline Ameriprise team-directory review

`ameriprise_team_capture.py` retains raw public directory responses and their
hashes. An HTTP 200 response titled `Request Rejected` is an access denial, not
an empty valid team. Denials stop acquisition; a parser correction is not
permission to resume a previously denied host.

`ameriprise_team_review.py` replays those files without network access. For
example, with paths beneath `data/refresh`:

```powershell
python src/ameriprise_team_review.py --capture CAPTURE_DIRECTORY --base BASE_CSV --base-receipt BASE_RECEIPT_JSON --output NEW_REVIEW_DIRECTORY --allow-incomplete --route-file PUBLISHED_NESTED_URLS_JSON
```

Use `--allow-incomplete` only to inspect partial evidence. The receipt remains
`partial_capture_not_source_accepted`; this option never waives missing pages,
rejected requests, locator count discrepancies or source acceptance. A live
capture cannot be replayed, altered bodies/base snapshots fail verification,
and original contact fields remain unchanged.

Same-card explicit root/biography links can establish source-local continuity.
Unique email plus compatible name can add source claims, not CRDs. Distinct
people publishing a shared mailbox remain separate and flagged. Named staff
cards without emails remain source records, not invented email recipients.
New card records receive no fabricated individual office or provider ID.

Encoded email display fragments are retained separately from a complete
destination on their own enclosing link. Two independent conflicting complete
destinations still require review. Parser/reconciler code hashes, body hashes,
original observation times, directory outcomes and unresolved profile routes
are retained in the review output.

These previews are not operational contact catalogs. Source-only fields such
as `source_email_scope` must not be treated as email/CRD approval; shared or
conflicting mailbox claims need explicit downstream disposition before adoption.

### Merrill relationship reconciliation

`merrill_relationship_reconcile.append_relationship_people` adds only explicitly
published provider IDs with their own verified person documents. It preserves
all existing rows and uses no parent's address, email, name or inferred CRD.
Source-marked-closed people and nonperson/team roots are retained in a separate
review, not interpreted as retirement. A current own-root record linked only
from closed parents receives a relationship-review flag. Private previews and
historical comparisons do not replace source acceptance or recipient checks.

### Reviewed Raymond James retries and cookie cleanup

Both Raymond James collectors explicitly clear cookies from their own isolated
browser context at the end of a run, then close that context. This does not
clear ordinary Chrome cookies, log the user out, export cookies, change browser
identity or affect user tabs. Closing the temporary context remains mandatory
even if the explicit cookie-clear call fails.

The locator supports an opt-in, operator-reviewed retry policy:

```powershell
python src/raymond_james_async.py --only "Boston, MA" --max-locations 1 --output-dir data/refresh/MY_REVIEW --fresh --denial-retries 3 --retry-delay 60
```

The default remains zero denial retries. Three retries means at most four
attempts at a city search that never succeeds. A verified successful city search
resets the budget for the next search. Each attempt completes isolated-session
cookie cleanup before another attempt; a delay of at least 60 seconds applies.
Previously saved city results remain in the checkpoint and dataset rather than
being erased by `--fresh` on a retry. Attempt outcomes and waits are recorded in
`raymond_james_retry_attempts.json`.

This reviewed policy retries ordinary HTTP 403 failures only. Authentication
failures, rate limits and human-verification challenges stop; `Retry-After` is
retained for a later compliant attempt. It does not automatically bypass a
challenge or change IP, identity, permissions or authenticated state. A limited
city canary is not proof of national roster completeness. The separate branch
collector still stops on denial; its cleanup alone does not enable retries.

Known network/transport timeouts also use the reviewed locator retry budget.
`--resume-reviewed-retries` explicitly continues an existing journal without
`--fresh`; attempts already spent are preserved. An incomplete attempt cannot
be resumed until its worker is verified terminal and its disposition reviewed.
Diagnostic screenshot/HTML/control calls have five-second limits. Owned-cookie
cleanup has a five-second limit and context closure ten seconds; inability to
verify context closure stops without automatically creating another session.
Browser-based captures sharing Chrome must run serially. Independent offline
validation and nonbrowser sources can still run in parallel.

### Merrill native fields and portable replay

The public source explicitly supplies `c_advisorNickname` and address `line2`.
`merrill_async.normalise` now preserves these as `Published Nickname` and
`Street Line 2`, and includes them in the exported columns. A native preferred
name is not an ACT override, a global nickname rule or a CRD/person approval.
Only an explicitly typed branch/office may provide an enclosing office name;
an advisor or team name is not an office label.

`merrill_native_fields.py` can replay omitted fields from the exact original
directory or selected member/own-root records:

```powershell
python src/merrill_native_fields.py --base PRIVATE_CANDIDATE --base-receipt PRIVATE_RECEIPT --directory-proof DIRECTORY_PROOF --lineage SOURCE_LINEAGE --scoped-receipt SCOPED_RECEIPT --extra-root-manifest ROOT_MANIFEST --baseline data/raw/firm_rosters/merrill_YYYY-MM-DD.csv --output data/refresh/NEW_PRIVATE_REVIEW
```

It requires the original candidate/manifest/body hashes and exact publisher IDs.
Existing names, emails, phones and identifiers remain unchanged. A missing suite
is filled only when its native street, city, state and postal code agree with
the existing whole office context. Conflicts are reported, not mixed into an
address. Output remains a private preview and does not waive historical holds.

The directory reader verifies UTF-8 JSON, unique IDs, count and the native byte
hash in one streaming pass. Directory-reference discovery uses the same reader,
avoiding repeated full-cache hashes. Exact native caches can be archived with
`merrill_native_fields.archive_directory_cache`; plain and gzip input are both
supported. The portable proof preserves original proof fields and the
uncompressed byte hash. Archive time must never be presented as retrieval time.
This keeps evidence available after temporary workspaces are cleaned without
copying a large uncompressed cache onto the network share.

Exact prior-root API records can outlive their published profiles. Check those
omissions separately with `merrill_profile_placement.py --review PRIVATE_REVIEW
--output data/refresh/NEW_PLACEMENT_CAPTURE --allow-network`. The review input
contains `count` and `records`, with publisher-provided `Advisor ID` and
`Profile URL` fields. Requests are serial, at least one second apart, never
automatically retried, and restricted to HTTPS `advisor.ml.com` URLs and
same-host redirects. Access denials/challenges stop the run; every response is
retained with its timestamp and native-byte hash. It uses no Chrome session.

`merrill_placement_review.review_capture` verifies the source hash, finished
capture, response hashes, exact provider IDs and redirect chains offline.
A redirect to the general advisor directory is not a current person listing,
but does not exclude membership on a current team page. Neither a redirect nor
a 404 proves retirement. Even an exact provider ID
on a published page requires field review before adding it to a candidate.
These checks do not modify operational rosters or confer identity/send approval.

### Repeatable Merrill scoped assembly

`merrill_scoped_assembly.py` rebuilds a private candidate directly from the
verified directory archive, complete office index, all published office and
directory reference captures, and exact own-root captures:

```powershell
python src/merrill_scoped_assembly.py --directory-proof PRIVATE_PROOF --office-capture PRIVATE_OFFICES --reference-capture PRIVATE_OFFICE_TEAMS --reference-capture PRIVATE_DIRECTORY_EXTRAS --root-capture PRIVATE_DIRECTORY_ROOTS --root-capture PRIVATE_SUPPLEMENTAL_ROOTS --root-capture PRIVATE_RELATIONSHIP_ROOTS --output data/refresh/NEW_SCOPED_ASSEMBLY
```

The first root capture must cover exactly the verified directory. All inputs
must be terminal and hash-valid. Every published office/directory reference
must have an outcome; an explicit empty response remains unresolved rather
than being called a successful team. Directory facts take precedence; other
members use the latest whole exact-ID source record, including its explicit
preferred team. Parent contact details and addresses are never inherited.
Explicitly typed `ADVISOR`/`PROFESSIONAL_STAFF` members with published display
names are retained even if separate first/last fields are missing. Their source
display name is preserved, not split or invented; `Source Name Structure`
records the distinction. Untyped groups do not become people merely because
they have display text. Nonstandard person IDs with complete structured names
are also retained. Own roots
provide profile/status evidence and transitive relationship closure, not a
license to merge contacts or backfill old data. Unrelated roots and unobserved
second-hop references fail closed. Outputs bind source manifests and processor
dependencies and remain subject to historical/source review.

`merrill_async.main` now uses `merrill_scoped_refresh.run_refresh` by default,
not the legacy broad feed. Each network phase runs in its own child process;
it must finish before its evidence can be reused. The run retains phase logs,
native captures, source bindings, and a private candidate. Supplemental root
requests require exact IDs verified in retained parent documents; recursive
closure requests use the same check. A timeout, missing root, schema drift or
access denial stops without blind retries. Each provenance manifest is hashed
once, not once per person. Reference workers are bounded to one through three.

`python src/merrill_async.py --dry-run` shows the plan without network or writes.
`--skip-teams` is rejected because it cannot provide complete scoped coverage.
Optional `--scoped-output` must name a new directory inside `data/refresh`.
Unresolved or invalid publisher references hold export for source review,
leaving the last-good roster untouched. Only a complete, reference-clean
candidate can be exported automatically, and a different same-date roster is
never overwritten. Historical/source comparison gates still apply afterward.

For a small live check without a full-roster claim:

```powershell
python src/merrill_directory_capture.py --output data/refresh/NEW_CANARY --canary-region AS --allow-network
```

A canary retains the national first page and requested regional slices, but
does not emit a full-directory proof. Full census collection checks stable
per-slice counts and unique IDs against the national total, retaining every
native response and deriving a compressed exact-object cache with provenance.
Explicit empty regions are valid; failures are never empty successes.

For profile selection, an exact own-root modern Merrill URL expressly marked
active can supersede a legacy-host link when the root is explicitly open and
person-typed. Unknown/negative activity, closed roots, provider-ID placeholder
paths and existing modern primary links are not overridden by this preference.
Publisher activity is not independently verified employment or HTTP availability.

The October Merrill review found real staff on current team pages whose
individual profiles redirect to the general directory. Do not use individual
profile availability or missing structured name fields as the sole membership
gate. The source-only acceptance receipt binds the exact candidate, native
assembly, independent reproduction, full baseline-omission CSV, published staff
placement, secondary reference outcomes, and unresolved mailbox/root/branding
reports. It does not approve CRDs, recipients, ACT writes or operational adoption.

Ameriprise offline review can combine disjoint, stopped capture folders with
`--additional-capture` and the original hash-bound `--target-csv`. It reads native
bodies in place, rejects duplicate/out-of-scope URLs and live input captures,
and preserves every original manifest and observation time. Complete target
accounting does not turn a rejected page into a successful observation.

### Edward Jones access failures and reviewed resumes

Profile HTTP 401, 403, or 429 now stops enrichment immediately after the
response is checkpointed. The collector does not retry that denial or renew
publisher cookies to try it again. Transient server/transport errors retain
bounded backoff. Queued workers are canceled, and no partial roster replaces
the existing CSV.

A denied checkpoint blocks both enrichment and a full run before new
discovery, including when --no-resume is supplied. The --reviewed-access-resume
option is an explicit operator decision only after publisher access/rate-limit
review; automation must never add it to get past a failed run. It does not
disable new denial stops or shorten a recorded Retry-After deadline. Keep the
original denied checkpoint and its evidence.

Bootstrap failures now log safe requested/final origin-and-path, HTTP status,
elapsed time, failure category, and canonical Retry-After when present.
Diagnostics omit query strings, fragments, cookie values, other response
headers, raw exception messages, and browser-page contents. HTTP-200 denial
titles and insecure/off-publisher redirects cannot export publisher cookies.
Only the collector-created bootstrap page is closed; ordinary Chrome contexts,
tabs, and cookies are not cleared. A missing old status cannot be reconstructed
retroactively: retained profile 429s are rate-limit evidence, while an older
generic bootstrap error remains an unknown bootstrap response.

The former default /us-en/find-a-financial-advisor returned HTTP404 in the
October26 diagnostic. The shared BOOTSTRAP_URL now uses the publisher's
[advisor finder](https://www.edwardjones.com/us-en/search/financial-advisor/results).
The replacement route and one previously limited profile returned HTTP200 in
separate diagnostics. This is not a full-acquisition success or proof of a
sustainable rate. A recovery trial must retain the original denied checkpoint
and bind any new successful result to its actual source observation; never
erase old failures or relabel cached facts as newly downloaded.

Successful enrichment now retains the exact completed checkpoint under a
content-addressed filename alongside the original checkpoint path. This keeps
per-profile observation times, response-body hashes and prior attempt history,
which the final CSV alone does not preserve. The active resume filename is
removed only when its bytes are retained in that archive. An inconsistent
pre-existing archive stops cleanup and leaves the resume evidence intact.
These are private audit artifacts, not application uploads. Do not edit them
or treat completed source observations as person/CRD or recipient approval.

### Edward Jones branch mailbox selection

Acquisition and contact loading share `src/edward_jones_email.py` for Edward
Jones only. Other firms retain their existing selection policy. Branch arrays
are not ownership-ordered and must remain intact. Prefer unique full-name
evidence, including numeric mailbox suffixes, punctuation and known nicknames.
The same source record's personal profile route can corroborate formal versus
calling names; it does not create global nickname aliases or CRD links.
Unseparated initials require explicit name/profile evidence: Joe is not Joel.
Equally name-supported mailboxes remain unselected and enter source review.

The October 26 reviewed candidate is separately staged, not written over the
raw country CSV. Its only changed original field is derived `email`; selection
reason/evidence columns are added. Native names, addresses, phones, branch IDs,
candidate arrays and biographies are preserved and independently checked.
Finder redirects do not establish retirement. Retained legacy observations
without timestamps/body hashes remain explicitly unknown, not relabeled fresh.
Source-ready status still does not authorize operational adoption or release.


### Saturday scheduled refresh

`src/refresh_schedule.py` dispatches the existing supervised workflow. Weekly runs
use `act-only`; quarterly runs use `full`. Both rebuild their dependency graph,
including contacts, map/search assets, ACT lookups and recipient shards, run the
Python/API/application checks, and prepare private release packages. Publishing
remains a separately approved operation. No held acquisition is auto-approved and
no failed source is silently replaced with older data.

Install with `scripts/install_refresh_schedule.ps1`, supplying the operational
repository, a durable `StageParent`, and the Python executable. It registers
one task, `Advisor Map - Saturday Data Refresh`, with no overlapping instances,
missed-start handling and wake-from-sleep enabled. It runs as the current Windows
user with an interactive token: the user must remain logged on (a locked session
is fine). A powered-off or logged-off workstation cannot run this task. A dedicated
Windows runner is the later option if this workstation availability is insufficient.

The local launcher records private logs in the user profile under
`AppData/Local/EIC/AdvisorRefresh/schedule-logs`. Scheduled workspaces use
the durable network share to avoid filling this workstation's small system disk. Run summaries
remain under `data/refresh/runs`, and `data/refresh/scheduled/history.json` records
scheduled outcomes. The dispatcher and supervised full-refresh launcher share an
OS lock. Never delete lock files to force a second worker. Quarterly retries use
the same run identity; a held quarter does not prevent the next weekly ACT job.
A missed quarter can be attempted on the next available start, after the schedule's
activation date. Browser handoffs remain visible and require operator follow-up.

ACT credentials use the existing environment or a scoped read of the existing
application's ACT settings through the scheduled user's Azure CLI session. Passwords
remain in memory and are never written to arguments, task XML, logs, or configuration.
An expired sign-in or missing credential produces a visible failed run requiring
attention. The scheduler does not change Azure settings or ACT records.

Complete the published-baseline adoption checkpoint above after an approved release
so later weekly builds select the intended source snapshots. A staged candidate and
its source-ready roster files do not perform that adoption automatically.
