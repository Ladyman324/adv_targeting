# ACT, SEC, and scraped-contact identity resolution

## Authorities and safety boundary

Production CRM data comes only from the newest paired ACT API pull:

- `data/raw/act_contacts_<UTC>.json`
- `data/raw/act_eic_contact_<same UTC>.json`
- `data/raw/act_pull_manifest_<same UTC>.json`

`CRM_Contacts_*.xlsx` is retired and supplies no production field. Missing
ACT API phone values remain blank; the stale workbook is never a donor.
`EIC_Contacts.xlsx` is a different, intentional 16-person internal directory.
The old `src/export_contacts.py` trial command is disabled so it cannot
overwrite the national contact artifact.

The fuzzy crosswalk is candidate evidence only. It cannot approve an ACT CRD,
recipient, ACT history read, or ACT write. Those actions require a unique
`identity_status == approved` ledger row.

## Evidence used

The resolver evaluates every useful identity field, without treating a fuzzy
similarity as proof:

- ACT: GUID, active status, asserted CRD, full and structured names, suffix,
  salutation, primary/alternate emails, business/mobile/alternate phones,
  company, title, full business address, tier, edited time, and relationship
  owner.
- SEC: legal and used names, suffix, current and prior employment, firm CRDs,
  and filed branch street/city/state/postal address.
- Scraped firm roster: exact email, name, phone, firm CRD, location, and team.

Hard conflicts include duplicate GUID, duplicate CRD, duplicate email,
inactive ACT record, invalid/missing SEC CRD, surname/given-name/suffix
conflict, and shared/generic mailbox. Human decisions are bound to the exact
evidence hash. Inactive records cannot be manually promoted.

## Eight-step operating sequence

1. Pull a paired immutable ACT snapshot:

   `python src/act_pull.py --user bladyman@eicatlanta.com --db EQUITYINVESTMENT`

2. Build review candidates (never an approval authority):

   `python src/act_crosswalk.py`

3. Build the versioned evidence ledger:

   `python src/build_identity_ledger.py`

   The manifest hashes the ACT and SEC inputs, crosswalk, source/evidence/link
   outputs, decisions, ruleset, row counts, and status counts.

4. Generate the colleague report:

   `python src/export_act_crd_corrections.py`

   Required workflow sentinels (currently Chris Tolman) are pinned by default.
   Use `--include-act-id <GUID>` to add another known case regardless of
   ranking.

5. Have the colleague edit only the decision columns in
   `data/identity/act_crd_corrections.xlsx`. Verify any proposed CRD in IAPD.
   A blank proposed CRD means the evidence found a problem but no safe
   alternative; it is not permission to reuse the current CRD.

6. Validate and apply reviewed decisions:

   `python src/import_act_identity_decisions.py <reviewed.xlsx>`

   `python src/import_act_identity_decisions.py --apply <reviewed.xlsx>`

   The first command is a dry run. Stale evidence hashes, missing reviewers,
   invalid actions, and unknown CRDs are rejected. Applying a decision updates
   the local decision ledger only; it does not write ACT.

7. Rebuild all local consumers in this order:

   `python src/build_identity_ledger.py`

   `python src/export_act_crd_corrections.py`

   `python src/build_act_economic_links.py`

   `python src/build_act_assets.py`

   `python src/build_contacts.py`

   `python src/build_field_tiles.py`

   `python src/build_name_index.py`

   `python src/build_advisor_search.py`

   `python src/build_act_lookup.py`

   `python src/build_act_mail_codes.py data/raw/act_contacts_<same UTC>.json`

   `python src/export_approved_recipients.py`

   `python src/web_assets.py`

   The economic-link build reads the exact ACT JSON named by the identity
   manifest and verifies its SHA-256 and row count before attributing assets.
   It may approve strict ACT identity links, exact unique validated
   ACT-to-roster emails, and strict one-to-one SEC name/location/current-firm
   residuals. These links are economic only: they cannot authorize email,
   calls, preferred names, ACT synchronization, or a CRD write-back.
   `build_act_assets.py` immediately consumes that hash-bound ledger and
   publishes only approved links whose CRDs exist in the deployed advisor
   index. Its client-side account table contains only that map-addressable set;
   approved off-map and unresolved source-book totals remain reconciliation
   metadata rather than anonymous client-side account vectors. The full CSV
   review export is research-only; no importer consumes edits to that report.

   The contact build validates and publishes the exact ledger/ACT provenance.
   The ACT lookup is ledger-approved only. The email registry is bound to the
   identity manifest, links, ACT source bytes, and contacts hash. `confirmed`
   and `high` identities enter the outbound registry by an explicit business
   authorization decision. `review` remains unresolved and is excluded from
   the controlled composer. Saved lists keep current-build proofs so a stale
   address or an old tier rule is re-evaluated before controlled sending.

8. Test, review, then deploy in the safe order:

   - run Python and API tests plus `python src/audit.py`;
   - upload the immutable
     `approved_recipients/releases/<registry-hash>/shards/*.json.gz` objects and
     their release-specific `manifest.json`, with a fresh explicit production
     approval;
   - publish static assets, including the anonymous `/review.html` sign-in
     relay used by scheduled-batch review notifications;
   - publish the Function App last; it is release-bound to that exact immutable
     manifest and cannot discover a different release by accident.

   `python src/export_approved_recipients.py --upload` performs only the safe
   first step. It deliberately leaves the legacy `approved_recipients.json.gz`
   untouched, so the currently running pre-shard Function App and an emergency
   rollback keep reading the release they were built against.

   The exporter also writes a PII-free shard manifest and registry release
   descriptor into the API package. The Function package preflight refuses to
   build unless that descriptor exactly pins the local registry, shard manifest
   and ACT lookup. Runtime rejects a validly self-hashed manifest or shard from
   any other release; all registry failures fail closed. Scheduled sends queue
   their safety check for the exact send instant, retry transient cold-start or
   storage failures three times, and use the repair timer only as a fallback.

## Email authorization and evidence

The tiers answer different questions and must not be described as equivalent
proof:

- `confirmed` means a source asserted the CRD and the source identity agrees
  with the SEC record. ACT-derived routes additionally require one unique,
  approved ledger GUID/CRD/email tuple.
- `high` means the roster-to-SEC scorer found a sufficiently strong,
  sufficiently unambiguous name/firm/location match. There is no asserted CRD.
  The business has deliberately authorized these routes for email despite that
  probabilistic evidence.
- `review` means the identity remains unresolved. It is visible as research
  evidence but is excluded from the registry and from the controlled composer.

The relevant contact calibration currently labels 633 of 200,949 contact rows
(0.32%). At the shipping thresholds it accepts 467 labelled rows, and all
467 match the independently published Barron's CRD. This is encouraging, but
it is not a 100% population guarantee: the labelling bridge preferentially
reaches cleaner records, Barron's skews toward large firms, and
`src/contact_calibrate.py` explicitly calls the precision an optimistic bound.
The 98.9% figure in `docs/gate_evaluation.md` measures a different question -
the precision of an ACT first-name rule when deciding which rows to demote -
and must not be used as the accuracy of scraped high-tier matches.

Every exported route carries its tier and, when the contact artifact supplies
one, `matchScore`. The score is bounded to the matcher's actual 0-1.18 range;
the suffix bonus can take it above 1.0, so it is evidence from the model rather
than a probability.

The raw Outlook `mailto:` action is intentionally discretionary and clickable.
It hands the displayed address to Outlook and does not claim the registry,
suppression, pacing, review, logging, or send guarantees of the controlled
composer. Identity warnings and the do-not-call state remain visible so the
rep makes that one-off decision knowingly. This escape hatch must not be
described as controlled email authorization.

### Source-level quality reporting

Aggregate calibration must not be silently inherited by every roster source.
The exporter now prints PII-free counts by tier and source, match-score coverage,
and ineligibility reason so each release exposes its population mix. A genuine
source-quality report should add, for every source, labelled count, correct
count, population and labelled coverage, a confidence interval, evaluation
date, and the hashes of the roster/contact/scoring artifacts. Sources with no
independent labels should say `unmeasured`, not borrow the aggregate precision.
Until that truth set exists, source counts are blast-radius reporting, not a
source accuracy score.

The API also supports the comma-separated
`APPROVED_RECIPIENT_BLOCKED_SOURCES` per-source emergency block setting.
Entries are normalized source names and block only `high` routes from that
source; a `confirmed` CRD from the same source remains eligible. Blocking a
source is an operational stop, not a deletion or a claim that the remaining
sources are accurate. The next source-quality report should record which
setting was active for the release.

## Preferred names

Preferred greeting/display names are separate from the legal identity link.
Strict common forms such as Christopher/Chris can approve automatically after
the ACT identity is approved. They may also be used as a presentation-only
overlay on an independently authorized roster route when every stricter gate
in src/preferred_names.py agrees: active ACT record; unique, personal primary
email; no asserted ACT CRD; unique high-tier CRD candidate at score 1.0 and gap
at least 0.25; exact surname/name/roster email; current roster firm, email
domain and SEC firm agreement; no hard conflicts; and either a strict nickname
or the SEC's own used first name. Alternate ACT emails are never considered.

The overlay never supplies routing authority, never attaches an ACT GUID and
never authorizes an ACT write. The legal/formal name remains visible as
Christopher (Chris) Tolman; only the greeting uses Chris. Other forms,
including Robert/Bo, still require a hash-bound human decision.

Current named examples:

- Robert Ladyman / CRD 4996584 is approved and displays/greeted as Bo.
- Chris Tolman's ACT record remains unmatched for CRD/write purposes. Its exact
  primary email and preferred Chris now overlay the independently authorized
  UBS route for CRD 2066775. It displays as Christopher (Chris) Tolman, greets
  as Chris, and still carries no ACT GUID or ACT write capability.

## Colleague workflow

The colleague should continue correcting CRDs, but through the resolution
report rather than editing ACT opportunistically. The report preserves the
ACT GUID, current and proposed CRDs, all corroborating/conflicting evidence,
the evidence hash, and the exact identity-manifest/ACT-source hashes. Generation
refuses ledger files that do not match the manifest. After review, import the
workbook and rebuild. Only a separate, reviewed ACT-write process should change
ACT itself.
