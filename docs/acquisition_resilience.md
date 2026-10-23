# Acquisition resilience implementation plan

Build resilient, source-aware acquisition with historical comparison, last-known-good fallback, and supervised AI repair proposals. Pilot the complete workflow on one firm roster before extending it to other rosters, rankings, and SEC feeds. Deliberately test broken formatting, partial downloads, missing fields, and large unexpected changes, and prove that they cannot silently replace good production data.

This is a separate goal from the completed refresh framework. Implementation and verification are local. Deployment, publication, and recurring production jobs require separate approval. No fresh external acquisition or model repair job has run under this goal yet.

## Acceptance requirements

| Requirement | Required evidence | Current status |
| --- | --- | --- |
| Source-aware historical comparison | Same-person comparisons, coverage and field checks, explicit source policy | Pilot plus provider-aware policies for 28 other existing firm sources, both ranking providers and all three SEC inputs implemented |
| Last-known-good fallback | Failed import leaves selected snapshot unchanged; build requires reviewed fallback | Controller integration test passes |
| Failure containment | Broken formatting, partial download, missing fields and unexpected changes are held | Pilot, generalized roster, ranking and SEC failure-injection tests pass |
| Useful diagnosis | Private immutable candidate evidence and safe aggregate report | Comparison reports and held bytes retained; early command failures create safe manual-review incidents |
| Supervised repair proposals | Bounded isolated runner, minimal code access, fixtures, protected validators, separate review | Tool-free proposal generation, bounded provider process, restricted fixture worker, independent operator decision, export and single-use retry authorization implemented |
| Complete roster pilot | Authorized small acquisition or replay, hold, diagnosis, proposal, validation, approval and new pinned run | Complete disposable-repository replay passes with actual staging/worker and mocked provider; no live fetch or model call |
| Broader source support | Roster schemas, ranking edition/category coverage, SEC archives/XML/feed continuity | Firm roster, ranking and SEC history integrated into import, registration and review |
| Final verification and handoff | Full regression tests, requirement audit, operator instructions, no silent publication | 612 Python and 720 API tests pass; completion audit below records scope and release boundary |

## Edward Jones pilot

Use the personal `fid`, not the shared branch/entity ID, as the historical comparison key. Compare each pull with the selected prior roster. Names and email addresses are values to inspect, not identifiers with which to guess that two records are the same person.

`src/refresh_resilience.py` currently checks:

- Strict CSV structure, required columns, duplicate columns, truncated rows and empty inputs.
- Missing or duplicate personal IDs and missing names or profile URLs.
- Invalid populated email values and unfinished or unrecognized profile extraction statuses.
- People retained, added and missing, even when total row count stays the same.
- Per-person name, email, phone, address, city, state and profile changes and losses.
- New email-to-person associations and newly shared email addresses. Existing shared associations remain visible in the baseline without being falsely reported as new changes.
- State-level coverage loss that could be hidden in national totals.
- Candidate and baseline checksums, including a second binding check immediately before registration.

Initial policy limits hold more than 5 percent missing people, more than 20 percent growth, more than 5 percent field loss, or more than 10 percent changes to a tracked field among retained people. State counts of at least five people are checked for losses greater than 20 percent. Any new association of an existing email with a different source person ID requires review. These are conservative pilot limits, not universal business rules or proof that a held change is wrong.

The older `--reduction-reason` option cannot override the pilot historical gate. A missing baseline requires review. A missing person is not automatically a confirmed departure. Exact-snapshot operator decisions can accept genuine changes or establish an initial baseline, but cannot override malformed CSV, ambiguous IDs, invalid email values, missing required person fields, or unfinished profile acquisition.

Reports live under the isolated stage at `data/refresh/acquisition_reviews/edward_jones/<comparison-id>/comparison.json`. Rejected candidate bytes live beside the report as `held.csv`; they are private data and never part of a repair prompt by default. The selected raw roster remains unchanged. Failed source attempts require explicit reviewed last-good fallback before a build can continue.

## Initial historical verification

The September 22 roster compared with itself has 19,781 retained people, no tracked field changes, and passes the gate. Comparing July 31 with September 22 retains 19,476 people, adds 305 and omits 181. It has 18,852 people with no tracked field changes. Nine new email-to-person associations and one newly shared email address cause that historical transition to be held for review; those flags are not a conclusion that the underlying firm data is incorrect.

The existing roster files were read without modification. These comparisons are historical replay, not a fresh retrieval or a freshness update.

## Other firm roster policies

`src/refresh_history.py` checks every registered roster source using its actual headers. Provider IDs are preferred, followed by profile-plus-name evidence, with name/location or practice-level continuity explicitly identified as weaker. These keys are for comparing source pulls, not creating advisor/CRD or ACT links. The policies preserve multiple offices for one provider entity and grandfather already-present duplicate rows, while holding new duplicate growth or newly conflicting names on a provider ID.

The checks separately compare names, emails, phones, city, state, address, title, team and profile coverage. They hold large entity disappearance/growth, changed field values, localized state loss, staff/advisor segment loss, and new email associations where provider/profile evidence supports that comparison. Current generic limits hold more than 20 percent missing source entities, more than 25 percent added entities, more than 5 percent field-density loss, or more than 10 percent changed values among retained entities. State/segment groups of at least five records are checked for losses greater than 20 percent.

Unlike the deliberately strict Edward Jones bootstrap pilot, a first snapshot for another firm can pass existing schema/usability checks with an explicit initial-snapshot warning; it cannot claim historical continuity without a baseline. There is no older selected snapshot to overwrite in that case. Missing entity identity or malformed CSV still holds the candidate. Existing-baseline holds cannot be overridden by a generic reduction reason.

All 28 existing non-pilot firm snapshots pass self-comparison. This checks compatibility with their actual current formats, not whether every existing contact record is correct. Fourteen source-policy tests cover provider IDs, multiple offices, duplicates, field/segment/geographic losses, email reassignment and explicit fallback evidence. The pilot's AI proposal permissions remain restricted to Edward Jones; other firms' holds use source-scoped human review rather than permission to edit unrelated scrapers.

## Ranking history policies

Ranking imports freeze requested edition metadata and categories before historical comparison. Both Barron's and Forbes compare category, full geographic/market scope, edition and source-native recognition identity. Explicit category subsets are respected; otherwise missing previously represented categories or geographies are held. New editions compare coverage and field completeness without requiring the same winners. Same-edition pulls additionally check missing recognition records. New duplicate/conflicting recognition growth and losses of rank, firm, location or provider identifiers are held separately from total row count. Existing legacy duplicates are reported without silently deleting them.

Barron's existing 1,800-row snapshot passes across 54 groups and its 2025/2026 editions. Forbes' 11,558 rows pass across 113 groups, with 11,552 distinct recognition identities; its absent edition remains unknown rather than being relabeled as the current year. These are historical self-comparisons, not fresh acquisitions. Fourteen ranking-policy tests cover new-year membership changes, same-year replacement, missing categories/geographies, field loss, duplicates, unknown editions, immutable history and the real held-import/review/accept flow. Older recognition remains retained.

Generic comparisons bind the exact baseline file set and requested scope as well as the candidate bytes. Ranking baselines include the staged canonical union and immutable editions, so newly accepted edition inputs remain visible even before a build materializes the union. Source publication dates are never inferred from local file modification times.

## SEC historical policies

The individual and firm XML feeds compare native CRDs, names, employment and location fingerprints, field coverage and archive-part counts. Individual names include alternate names. Separate metrics cover registrations, employment history, examinations, designations, disclosures and other businesses. XML is streamed rather than retaining the entire feed in memory. Both individual lowercase and firm capitalized address attributes are recognized.

ADV checks native firm CRDs, required columns, usable asset values and aggregate reported regulatory assets. Numeric and flag checks follow the application's ADV consumer, including client mix and allocation fields. Completeness checks also cover the retained raw columns. A dollar-formatted number that the consumer would discard cannot pass as a usable asset value. Aggregate reported regulatory assets are a change diagnostic, not a deduplicated estimate of economic assets.

Current SEC limits hold more than 2 percent missing CRDs, more than 25 percent growth, more than 5 percent field-density loss, more than 20 percent changed record fingerprints, or more than 50 percent change in aggregate reported regulatory assets. Any archive-part count change requires review, even if it is legitimate repartitioning. Rare raw, positive-value and affirmative-flag fields need at least 25 populated baseline records for density-loss checks. Other tracked metrics require at least one. A first snapshot without history carries an explicit warning rather than claiming continuity.

Corrupt or truncated archives, duplicate or absent native CRDs, unsupported XML namespaces, wrong feed types, DTD/entity declarations and malformed CSV cannot be approved through an operator override. Archive size bounds are checked before CRC decompression. A well-formed but unexpectedly partial XML file is still held by historical comparison.

ADV selection uses the exact `ia??????.zip` filename shape. The former broad `ia*.zip` pattern also matched the individual XML ZIP on Windows; a regression test proves an ADV import now leaves the individual feed untouched. Baselines use source dates, not lexical month order or file modification times.

Twenty-two SEC tests cover these policies and the real hold, review and import path. Existing local snapshots contain 16,779 ADV firms, 23,576 XML firms and 436,091 individuals across 20 XML parts. Those May and July 2026 files are historical calibration inputs, not fresh October acquisitions.

## Repair boundaries

`src/refresh_repair.py` creates private, idempotent incident requests from allowlisted aggregate diagnostic fields. Requests exclude raw HTML, contact lists, arbitrary exception text, ACT exports, credentials and filesystem paths. Candidate and baseline hashes, source adapter hash and diagnostic codes identify the incident. A repeated hold cannot overwrite an existing review decision.

Identity conflicts, coverage changes, transport failures and baseline questions go to manual review. Parser-like failures can await diagnostic review when the pinned adapter exists. No model is authorized or launched merely because an incident exists.

An acquisition command that fails before producing a candidate creates a separate incident under `data/refresh/acquisition_incidents/<id>/incident.json`. Its reference appears in the run attempt returned by `status`. These incidents contain only the source, a fixed failure/interruption category, and baseline/command hashes. They do not infer that a failed command was necessarily a network or parser problem. Exceptions, command arguments and logs are not copied into the incident. Repeated equivalent failures retain the existing incident. The selected data and retrieval timestamp remain unchanged; failure to write diagnostics cannot hide the original acquisition failure.

Structural proposal validation permits implementation changes only within `extract_emails`, `extract_bio` and `find_profile_url` in `src/edward_jones_async.py`. An AST comparison protects module imports, module execution, discovery validation, other functions, function arguments, defaults and decorators. Proposal generation and recording never execute or apply code. Separate restricted fixture validation can execute the parser subset described below. Invalid Python, semantic no-ops, stale source and second proposals are rejected.

## Operator review of held data

Inspect the private comparison report and source evidence before recording a decision. The local operator CLI requires a named reviewer, a reason, and explicit acknowledgment of every issue code. It recomputes the assessment against the currently selected baseline before saving an immutable decision. Changing the candidate, baseline, report, decision contents, or recorded decision hash invalidates acceptance.

For example, replace the placeholders and include every issue actually present:

```powershell
python src/refresh_data.py review-acquisition --run RUN_ID --comparison COMPARISON_ID --decision approve --reviewer "Operator name" --reason "Verified the supplier changed its published coverage" --acknowledge-issue PEOPLE_DISAPPEARED --acknowledge-issue STATE_COVERAGE_LOST
```

That command defaults to the Edward Jones pilot. For another source, supply its exact identifier, such as `--source roster:morgan_stanley`, `--source forbes`, `--source sec_adv`, `--source sec_firms` or `--source sec_individuals`. Review evidence is stored under that source's private `data/refresh/acquisition_reviews/<source>/` folder. Use the issue codes actually reported for that source. Invalid schemas, unsupported identities, or ambiguous provider IDs cannot be approved merely by supplying a reason. `python src/refresh_data.py status --run RUN_ID` exposes the failed attempt's comparison reference and issue codes without dumping contact data.

Use `--decision reject` to retain an explicit rejection. Decisions cannot be overwritten. Approval is an attestation, not a data selection or publication action. A subsequent explicit import must still validate and register the exact candidate. The approved review reason also satisfies the older aggregate reduction check, so the operator need not justify the same change twice. Approval does not clear the preceding failed acquisition status; only a successful import/acquisition can do that. The CLI trusts the operator's local filesystem access and reviewer attestation; it is not a public API identity or authorization mechanism.

## Text only proposal runner

`src/refresh_repair_runner.py` uses a single structured OpenAI Responses API request rather than launching a tool-enabled coding session. The model receives the pinned Edward Jones adapter, safe aggregate diagnostics, and an explicitly approved synthetic/public fixture. It receives no shell, browser, filesystem, deployment, or other tools. Held contact CSVs, ACT data and other workspace files are not read into its prompt.

This is an intentional implementation choice: a parser proposal does not require an agent to execute code. It avoids exposing host credentials through agent tools. Authentication uses `OPENAI_API_KEY` only at the fixed HTTPS provider call; the model must be explicitly selected by the operator. Saved Codex CLI sign-in alone is not API authentication. The HTTP client disables environment proxy configuration and redirects, sets a 45-second I/O timeout, makes no automatic retries, and limits the model output to 6,000 tokens.

The trusted HTTP implementation runs in a separate child with a 600-second total deadline. The supervisor kills and reaps a stalled child before cleaning up its temporary directory. Windows resource containment attaches before request input is sent. The child receives a minimal environment without ACT, Azure or deployment credentials; the provider key crosses a private stdin pipe, not a command line or saved input file. Streamed responses are capped at 500 KB. Local tests prove successful and failed child results and a real stalled child's termination without making an API call. Killing a local request does not prove the provider stopped processing it or incurred no charge.

The fixture JSON contains only `kind` (`synthetic` or `public-approved`) and one to twelve `cases`. Each case contains `html`, `expectedEmails` and `expectedBio`. Review fixtures for secrets before authorizing transmission. The schema and confirmation flag do not independently prove that supplied HTML is public or secret-free.

```powershell
python src/refresh_data.py propose-repair --run RUN_ID --request REQUEST_ID --fixture APPROVED_FIXTURE.json --model MODEL_ID --operator "Operator name" --reason "Reviewed the public fixture and authorized this parser proposal" --allow-ai --confirm-public-or-synthetic-fixture
```

Add `--dry-run` to inspect the command without calling a model or changing files. Actual calls record authorization, prompt/fixture hashes, and a running attempt before contacting the provider. Failed or interrupted calls consume their authorization and cannot silently repeat. Provider refusals, incomplete output, malformed JSON/Python, no-op proposals, changed source, and edits outside protected parser boundaries fail closed. Responses use a strict schema, no tools and `store: false`; disabling response storage does not imply every form of provider retention is disabled. See [Structured model outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses) and [Responses API migration](https://developers.openai.com/api/docs/guides/migrate-to-responses?api-mode=responses).

A successful call writes a separate immutable proposal awaiting validation and independent review. It never executes the proposal, edits the pinned adapter, selects contact data, heals an acquisition failure, or authorizes publication. Current verification uses a mocked provider; no actual API call has run.

## Explicit retry authorization

After inspecting a terminal failed attempt, an operator can authorize one more proposal attempt:

```powershell
python src/refresh_data.py reauthorize-repair --run RUN_ID --request REQUEST_ID --operator "Operator name" --reason "Reviewed the terminal failure and approved one more proposal attempt" --acknowledge-possible-charge
```

This command does not call the model. Then rerun `propose-repair` with the reviewed fixture and its normal authorization flags. Each retry requires its own immutable attestation, bound to the complete previous attempt history and unchanged adapter. An incident is capped at three total attempts. Earlier attempt authorizations and fixtures remain retained. Changing the history or retry decision invalidates permission; a completed proposal, existing proposal artifact or running call cannot be reauthorized. An uncertain provider call may still have been processed or billed, even after its local child was stopped.

Both commands support `--dry-run`. A hard supervisor crash that leaves a request marked running requires investigation, not a retry based only on an old timestamp. A new parser version requires a new pinned run and incident. Retry approval does not approve a patch, clear the acquisition failure, or grant deployment permission.

## Trusted validation and independent decision

Run the proposed parser against a separate fixture that was not supplied to the model:

```powershell
python src/refresh_data.py validate-repair --run RUN_ID --request REQUEST_ID --regression-fixture HELD_OUT_FIXTURE.json --timeout-seconds 10
python src/refresh_data.py review-repair --run RUN_ID --request REQUEST_ID --decision approve --reviewer "Independent reviewer" --reason "Inspected the parser changes and passing independent regression evidence"
python src/refresh_data.py export-repair --run RUN_ID --request REQUEST_ID
```

Each command supports `--dry-run`. Failed validation returns a nonzero exit code. The default worker wall-time limit is ten seconds, configurable from one to 45 seconds. Validation compiles only the supported parser functions, with restricted builtins, narrow library facades, fixed trusted profile helpers, and a capability allowlist. It does not import or execute the scraper module's imports or top-level statements. Imports, file/shell/network operations, introspection, arbitrary calls and unsupported syntax fail closed. Python audit hooks provide an additional external-operation guard.

The worker is a restricted parser language, not a general-purpose Python security sandbox. It runs in a fresh temporary directory with an isolated Python startup and an environment stripped of provider, ACT and deployment credentials. On Windows, a resource job attaches before proposal input is sent, limits the process to 256 MiB, allows one active process, and kills the worker when the supervisor's job handle closes. POSIX workers apply a 256 MiB address-space limit before reading input. Native Windows memory-denial and kill-on-close tests pass. See Microsoft's [job limit structure](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information) and [job assignment behavior](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject).

Passing requires a reproduced failure in the original parser, a successful fix in the proposal, and passing original/proposed results on held-out and mandatory historical email/biography/profile cases. Supplied regression cases cannot overlap model-visible cases. Validation saves immutable numbered evidence and regression fixtures, binds their hashes to the proposal and authorization, and records the actual worker PID, creation identity and exit status. Worker, controller and resource-limiter source hashes are bound too. Timeouts and containment-setup failures kill and reap the worker before Windows temporary-directory cleanup.

Independent approval requires a named reviewer different from the proposal authorizer, a reason and unchanged passed evidence. These are local operator attestations, not proof of separate authenticated user identities or automated semantic code review. A reviewer must inspect the actual proposed diff and evidence. Rejection is allowed without passed tests. Decisions cannot be overwritten; altered proposal, decision, fixture, worker or validation evidence blocks export.

Export writes `approved-change.json` containing the exact replacement, unified diff and source/decision/validation hashes. It never applies the change. Adopt an approved patch only into the source checkout under the appropriate authority, then start a new pinned refresh run and rerun acquisition validation. The old stage remains untouched and its controller-integrity check rejects reuse after the checkout changes.

## Complete pilot replay

`tests/test_refresh_resilience_pilot.py` exercises the real staging controller in a disposable repository with a valid native ACT fixture and 100 synthetic Edward Jones rows. It uses the current real parser against a deliberately changed HTML selector to create the missing-email pull. The historical gate holds it and preserves the selected roster. After a simulated provider failure, the CLI checks retry dry-run nonmutation and records an explicit retry attestation. A mocked provider supplies a parser-only repair; the real worker validates it; the CLI records a separate review-step attestation and exports the approved change. Source adoption occurs only in that disposable checkout. The old run correctly rejects the changed controller, a new run pins the new code, and repaired roster data passes the historical gate. No application datasets or deployment artifacts are produced. This is a complete local replay, not a live source scrape or paid model test.

## Operational handoff

Use the supported `refresh_data.py` acquire/import workflow to enforce these gates; running a legacy scraper directly does not automatically apply this historical review process. Start a new pinned run after any reviewed source change. Before a live repair call, review the public failure fixture and independent held-out cases; treat scraped content as untrusted data, not instructions.

Staged imports copy to temporary files, verify source and copied hashes, then rename into place. Interrupted or corrupted copies cannot leave a partial final selector. Registration rollback restores displaced last-good files. Failed attempts retain truthful failure receipts and cannot be presented as fresh data; an explicit fallback reason is required before building from retained inputs.

Implementation and local replay are complete. Source changes remain uncommitted and no new production release was created. Commit the reviewed source and create a new clean refresh run before preparing a production candidate. Do not reuse the earlier refresh-framework rehearsal artifact: it predates these resilience changes. Any live acquisition, paid model call, patch adoption, upload or deployment must remain within its separately authorized scope. Passing tests never authorizes weakening validation or inventing missing fields.

Proposal generation remains separate from code acceptance and publication. A tool-enabled Codex runner can be added later if needed, but is not required for the current tool-free provider path. See [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) for that alternative.

## Verification commands

Run from the repository root:

```powershell
python -m unittest discover -s tests -p test_refresh_resilience.py -v
python -m unittest discover -s tests -p test_refresh_repair.py -v
python -m unittest discover -s tests -p test_refresh_review.py -v
python -m unittest discover -s tests -p test_refresh_repair_runner.py -v
python -m unittest discover -s tests -p test_refresh_repair_retry.py -v
python -m unittest discover -s tests -p test_refresh_repair_validation.py -v
python -m unittest discover -s tests -p test_refresh_resilience_pilot.py -v
python -m unittest discover -s tests -p test_refresh_history.py -v
python -m unittest discover -s tests -p test_refresh_ranking_history.py -v
python -m unittest discover -s tests -p test_refresh_sec_history.py -v
python -m unittest discover -s tests
```

October 23 verification passes 612 Python tests and 720 API tests with no failures. Targeted coverage includes 18 pilot historical/import tests, 12 repair-boundary tests, 14 operator review/controller tests, 15 mocked-provider and supervised-transport tests, seven retry-authorization tests, 26 real-worker validation/review/resource tests, one complete roster replay, 14 general roster-policy tests, 14 ranking-policy tests and 22 SEC-policy tests. All three existing SEC inputs pass self-comparison with unchanged policy hashes during calibration. Production behavior and actual model calls remain outside this evidence.

## Completion audit

The local implementation goal is complete; this is not a production release approval. The following evidence was inspected and the referenced tests passed in the integrated suite.

| Requirement | Implementation and proving evidence |
| --- | --- |
| Resilient acquisition with source-aware historical comparison | `refresh_history.py`, `refresh_sec_history.py` and `refresh_resilience.py` enforce provider identities, per-field continuity, localized coverage and checksummed baselines. Roster, ranking and SEC policy suites cover stable, reordered and legitimately changed data as well as suspicious differences. |
| Last-known-good fallback | `refresh_acquire.py` gates before registration, archives inputs and rolls back registration failures. `test_complete_import_failure_then_recovery_preserves_live_baseline` proves held data is not selected and a build requires an explicit fallback reason. Acquisition tests prove failed retrieval does not update retrieval time. |
| Broken formatting cannot silently replace good data | Pilot and generic tests inject duplicate headers, malformed CSV and truncated rows. SEC tests inject malformed archives, unsupported XML and invalid identifiers; review tests prove structural failures cannot be overridden. |
| Partial downloads cannot silently replace good data | Pilot and ranking tests reject valid-looking partial sets. SEC tests reject truncated ZIP/GZIP and well-formed partial XML. Acquisition tests inject partial and corrupted local copies, prove no partial final selector exists and verify restoration of last-good bytes. |
| Missing fields and large unexpected changes are held | Tests independently remove emails, phones, titles, addresses, states, aliases, employment and ADV business fields; replace people while preserving counts; swap emails; lose ranking scopes; and change asset units. Each source uses its own policy rather than a shared row-count-only threshold. |
| Supervised AI repair proposals | `refresh_repair.py` limits changes to three Edward Jones parser functions and strips diagnostics to safe aggregates. Runner tests cover explicit authorization, refusal, incomplete output, invalid proposals, protected code and secret exclusion. The transport deadline test kills and reaps a real stalled child. |
| Trusted validation and independent acceptance | The restricted worker tests reproduce the old failure, pass the fix and hidden regressions, reject overlap and external operations, enforce resource limits and bind evidence hashes. Review/export tests reject self-approval, missing tests, stale code and altered evidence; export does not apply the patch. |
| Full roster pilot before broader rollout | The complete Edward Jones replay runs hold, failed proposal, explicit retry, proposal, real fixture validation, independent attestation, export, disposable source adoption and a new pinned run. It proves the old stage rejects changed source and repaired data is accepted only in the new run. |
| Extension to other rosters, rankings and SEC | All 28 existing non-pilot roster snapshots and both ranking snapshots were calibrated. All three native SEC inputs were calibrated. General source, ranking and SEC tests also exercise held import and exact-snapshot operator review. AI parser-edit scope remains Edward Jones only. |
| Useful diagnostics with bounded retries | Early command incidents contain only fixed categories and hashes, preserve last-good data and do not infer a parser defect. Seven retry tests prove explicit single-use authorization, preserved history, a three-attempt cap and rejection of altered evidence or existing proposals. |
| Separate goal and separately approved deployment | This runbook keeps acquisition resilience separate from the prior refresh framework. The workflow does not publish. Release tests require clean source and bound dependencies; the pilot proves no application datasets or deployment artifacts are produced. No live source acquisition, model call, Git commit, upload or deployment was performed for this goal. |

Self-comparison confirms compatibility with existing files, not the correctness of every contact or the freshness of those files. Local reviewer names are attestations backed by filesystem access, not independently authenticated user identities. The AI transport is tested with mocked responses; live provider availability and real-world repair quality must be evaluated during a separately authorized canary.
