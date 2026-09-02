# Trust-company research pipeline

This pipeline builds the ability to investigate trust companies without adding
them to the advisor map. It is deliberately separate from the SEC/CRD build,
contact identity registry, API package, static web assets, and email eligibility.

## Safety boundary

- All repository output must remain below
  `data/research/trust_companies`. That directory is ignored by Git.
- Every report and manifest says `productionEligible: false`.
- CRDs are retained as observations but are never institution merge keys. This
  prevents a terminated predecessor CRD from becoming a current trust-company
  identity.
- Exact names, locations, and domains produce review suggestions only. Automatic
  links require a shared RSSD, CIK, LEI, EIN, OCC charter, state charter, or Form
  13F file number and no conflicting strong identifier.
- Any conflict between strong identifiers fails closed.
- Known-case sentinels with an identifier must match that identifier; a matching
  name and state cannot substitute for a wrong CIK, RSSD, or other identifier.
- Inactive, surrendered, denied, revoked, and historical regulator rows remain
  visible as evidence but are excluded from the current regulated universe and
  prospect candidates.
- Generated CSV cells that could execute as formulas are apostrophe-escaped for
  safe Excel review. The unchanged input artifact and SHA-256 manifest retain
  source provenance.
- No production build entrypoint imports these modules.

## Supported sources

1. **FFIEC NIC active attributes**: use the official CSV ZIP from the
   [FFIEC data-download page](https://www.ffiec.gov/npw/FinancialReport/DataDownload).
   The adapter retains `MTC`, `NTC`, and charter type `250` records.
2. **SEC Form 13F quarterly data**: use an unmodified ZIP from the
   [SEC Form 13F datasets](https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets).
   A 13F filer with “Trust” in its name is only a candidate; 13F does not prove a
   trust charter. The adapter retains the latest filing per CIK in the archive.
3. **State or OCC regulator records**: normalize the authoritative roster into
   the generated regulator CSV template. Every row requires the regulator source
   name, regulator record identifier, legal name, and direct source URL.
4. **OCC active trust banks**: the official `trust-by-name.xlsx` workbook is
   parsed directly. Its embedded "Active As of" date must agree with any date
   supplied on the command line.

`research/trust_companies/source_registry.csv` records the authoritative URL,
format, cadence, retrieval method, and current local research artifact. A source
being official does not make every institution a sales prospect.

The source files are intentionally operator-supplied for the first research
phase. This gives us a chance to measure coverage and source stability before
automating downloads.

## First run

Create empty templates:

```powershell
python src\trust_company_research.py init-templates
```

Download the FFIEC and SEC ZIPs and place them somewhere under
`data/research/trust_companies/input`. Add normalized state-regulator CSVs when
available. Then build a report:

```powershell
python src\trust_company_research.py build `
  --occ data\research\trust_companies\input\occ_trust_banks_2026-07-31.xlsx `
  --occ-as-of 2026-07-31 `
  --sec-13f data\research\trust_companies\input\sec_13f_2026-03-01_2026-05-31.zip `
  --sec-13f-as-of 2026-05-31 `
  --known-cases research\trust_companies\known_cases.csv
```

SEC archive labels are filing-month windows, not calendar-quarter labels. Form
13F is an investment-activity signal, not proof of charter, client-facing work,
or assets under management. Since January 2023, its table value is in dollars.

`--registry` is repeatable. A failed known case returns exit code 2 so a missing
or unexpectedly ambiguous sentinel cannot go unnoticed.

## Regulator CSV contract

The required columns are:

- `source_name`
- `source_record_id`
- `source_url`
- `legal_name`

Optional fields include `as_of_date`, `institution_type`, `type_evidence`,
`status`, `regulator`, `charter_authority`, address fields, website/domain,
`source_snapshot_date`, `license_scope`, `public_private`, `rssd`, `cik`, `lei`,
`ein`, `fdic_cert`, `occ_charter`, `state_charter`, `form13f_file`, `crd`,
`sec_file`, and `notes`. A state charter without a state prefix is stored as
`<STATE>:<charter>`.

## Review outputs

- `source_records.csv`: normalized source rows with row/file provenance.
- `institutions.csv`: strong-ID clusters and research-only identifiers.
- `regulated_universe.csv`: the broad regulator-confirmed universe; it is not a
  map-import list.
- `prospect_candidates.csv`: transparent research triage with explicit inclusion
  reasons and exclusions for private-family or clearly specialized institutions.
- `proposed_links.csv`: automatic strong-ID links, review suggestions, and
  blocked conflicts.
- `unresolved_records.csv`: records not connected by a strong identifier.
- `known_case_results.csv`: sentinel coverage results, starting with Blue Trust.
- `summary.md` / `summary.json`: coverage and quality counts.
- `manifest.json`: SHA-256 hashes for every input and generated report.

## Evaluation before any product decision

The first comparison should answer:

1. How many institutions are found by FFIEC, 13F, and state sources?
2. Which large 13F managers lack regulator confirmation?
3. How many links are automatic, reviewable, unresolved, or conflicting?
4. Does the source supply headquarters only, or usable office locations?
5. How often do names, charters, and URLs change between refreshes?
6. Which authoritative source could eventually support professional rosters?

Only after those questions are answered should we design a separate people and
office layer. That later layer should keep trust-company identifiers distinct
from advisor CRDs and require its own territory and contact-safety review.

The first live release contains the SEC March-May 2026 archive and the OCC trust
bank workbook active July 31, 2026. FFIEC, FDIC, and state rosters remain listed
as explicit gaps until their official artifacts are supplied and normalized.
