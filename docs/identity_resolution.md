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

   `python src/build_contacts.py`

   `python src/build_field_tiles.py`

   `python src/build_name_index.py`

   `python src/build_advisor_search.py`

   `python src/build_act_lookup.py`

   `python src/build_act_mail_codes.py data/raw/act_contacts_<same UTC>.json`

   `python src/export_approved_recipients.py`

   `python src/web_assets.py`

   The contact build validates and publishes the exact ledger/ACT provenance.
   The ACT lookup is ledger-approved only. The email registry is bound to the
   identity manifest, links, ACT source bytes, and contacts hash. Only
   `confirmed` direct-CRD identities enter outbound email; calibrated `high`
   roster matches remain research/calling evidence and are not email authority,
   including external Outlook mailto links. Saved call lists keep separate
   current-build proofs for calling and emailing so high-tier research contacts
   remain callable without becoming email-authorized.

8. Test, review, then deploy in the safe order:

   - run Python and API tests plus `python src/audit.py`;
   - upload `approved_recipients.json.gz` first with a fresh explicit
     production approval;
   - publish the Function App second;
   - publish static assets separately.

   The exporter also writes a PII-free registry release descriptor into the API
   package. The Function package preflight refuses to build unless that
   descriptor exactly pins the local registry and ACT lookup. Runtime rejects a
   validly self-hashed blob from any other release; all registry failures fail
   closed.

## Preferred names

Preferred greeting/display names are separate from the legal identity link.
Strict common forms such as Christopher/Chris can approve automatically only
after the CRD identity is approved. Other forms, including Robert/Bo, require a
hash-bound human decision.

Current named examples:

- Robert Ladyman / CRD 4996584 is approved and displays/greeted as Bo.
- Chris Tolman's ACT record is pinned in the correction report but remains
  unmatched until CRD 2066775 is explicitly reviewed. The independently
  scraped UBS record remains visible as high-confidence research evidence, with
  no ACT GUID and therefore no ACT write capability.

## Colleague workflow

The colleague should continue correcting CRDs, but through the resolution
report rather than editing ACT opportunistically. The report preserves the
ACT GUID, current and proposed CRDs, all corroborating/conflicting evidence,
the evidence hash, and the exact identity-manifest/ACT-source hashes. Generation
refuses ledger files that do not match the manifest. After review, import the
workbook and rebuild. Only a separate, reviewed ACT-write process should change
ACT itself.
