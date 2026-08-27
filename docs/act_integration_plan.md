# Act! integration — plan

_Written 2026-08-13, after the first full API pull (47,466 contacts, 225 fields)._

## What we now know

The Act! Web API works. Endpoint is `https://apius.act.com/act.web.api`, database
`EQUITYINVESTMENT`, basic auth to `/authorize` returning a bare JWT. Tooling is in
`src/act_client.py`; `src/act_probe.py` exists to diagnose a failed connection.

Four findings shape everything below.

**The manual Excel export is not stale — it is narrow.** 47,426 rows against the
API's 47,466, and 40,704 distinct emails against 40,749. The reason to move off
`data/raw/CRM_Contacts_*.xlsx` is not freshness. It is that the spreadsheet
carries 14 columns out of 225.

**There is a book of business in the CRM that the pipeline never sees.** The
`b*` custom fields are a repeating per-account structure, up to 11 slots per
contact: `bname` (relationship), `bcode` (account number, platform-prefixed),
`bvalue` (total), `ballcap` (All-Cap), `blarge` (Large-Cap), `bcount`,
`open_date`.

The **bcode prefix names the platform** — MS, RJ, UB, ML, WA, EV, ST, PE, JA,
US, LP, SW — with one exception that names a PRODUCT: **`MF` is the EICIX
mutual fund**, whose value sits in the all-cap column. Mid-Cap Value has no
column and appears as account value with neither allocation, i.e. the residual.

**It must be de-duplicated by bcode.** A team account carries the SAME bcode on
every member's contact record: 1,494 of 3,747 accounts have more than one
holder, up to eight. Summing per contact overstates the book by **42%**
($14.8B against $8.5B) — the same trap `build_contacts.py` documents for the
CRM's single "Total Assets" column, and the reason the map stores team assets on
the team rather than the person. The bcode is what makes the correct version
computable, because it identifies the account.

De-duplicated (`src/act_book.py`):

| | |
|---|---:|
| All-Cap Value SMA | $5,933,346,439 |
| Large-Cap Value SMA | $2,385,993,676 |
| EICIX mutual fund | $200,960,145 |
| Mid-Cap / unallocated | $19,996,362 |
| **Total** | **$8,540,296,623** |

Product mix differs sharply by platform, which is a sales fact rather than a
data one: Merrill Lynch is $1.06B All-Cap and **zero** Large-Cap; Raymond James
is the mirror image at $791M Large-Cap against $199M All-Cap.

**The summary fields disagree with the detail.** `acv_total_assets` against
`sum(ballcap_*)` agrees 3,396 times and disagrees 277. Ten accounts carry
different values on different team members' records. Always recompute from the
per-account rows; never trust the rollup.

**Two custom fields are worth more than the CRD question that started this.**
`user5` is a platform code (MSWM, WFA, RJ, UBS, ML, Stifel, RBC, ENV, SWB, LPL —
31,415 populated, 66.2%), an independent second signal for firm attribution
where the pipeline currently infers firm from the email domain at 64%.
`product_interest` maps to our actual strategies (All 19,450, Large 10,404,
MF 1,508, Mid 309, SRI 227 — 25,951 populated).

### Two constraints

Webhooks are documented for eight entities but **not available on Act! Premium
Cloud**, so change detection means polling `edited` with an OData filter.

OData is GET-only and partial: `$filter`, `$select`, `$top`, `$skip`,
`$orderby`, `$expand` on multi-record reads, nothing on single records. Bulk
writes go through `POST api/$batch`.

---

## Phase 0 — DONE. The database documents itself.

`--fields` returned 190 definitions and 29 picklists, and the codes the census
could not read are described inside Act!.

### Mail Code (`email__y_n`) — the answer to "what does a blast reach"

| code | contacts | with an address | meaning |
|---|---:|---:|---|
| `2` | 22,195 | 21,954 | Email only |
| `P` | 15,996 | 15,724 | **Prospect, no mass mailings yet** |
| `N` | 4,947 | 1,350 | No mail; cannot locate; bounce backs |
| `U` | 3,064 | 1,774 | UNSUBSCRIBE!!!! |
| `NC` | 527 | 426 | No mail by request |
| `BB` | 277 | 277 | not in the picklist — presumably bounce-back |

**21,954 are mailable today. 15,724 more are held back by policy, not by
suppression** — "Prospect, no mass mailings yet". Only 3,845 are genuinely
suppressed. Against the 114,309 advisors with an email address in
`contacts.json`, an Act! blast currently reaches **19%** of the reachable
universe, and the largest single constraint is a deliberate choice about
prospects rather than a data problem.

### `USER5` is "Program Type" — the approved-platform field

27 codes, 31,415 populated: `OSC` Outside Single Contract, `ODC` Outside Dual
Contract, `MSWM` Morgan Stanley Wealth Management, `WFA` Wells Fargo Advisors,
`RJ` Raymond James, `UBS`, `ML` Merrill Lynch, `Stifel`, `RBC`, `SWB` Schwab,
`ENV` Envestnet, `VSTMK` Vestmark/Janney/Adhesion/Dynasty, `PER`
BNY/Pershing/Stephens/Arvest/Sanctuary/…, `SRX` SMARTRX, `LPL`, `FID`, `USB`,
`WIL`, `PRIN`, `TCV`, `Orion`, `RIA`, plus `EIC` (staff), `H` historical,
`News`, `O` other, `P` prospect.

This is the structured form of the approved-platform knowledge the earlier
segmentation discussion assumed only lived in the sales team's heads.

### `Has Clients @ EIC` — 4,214 lapsed relationships

`Y` Current Clients 4,080 · **`F` Had Clients in Past 4,214** · `N` No Current
Clients 21,659 · `D` Quarterly Distribution 932 · `G` Assets with Group 420.

The 4,214 who *used to* be clients are a prospecting list that nothing in the
current tooling surfaces.

### Also decoded

`USER1` Class: `A` Allcap, `L` Large cap, `M` Midcap. `USER3` Contact Code
records which relationships predate which regional director. `USER10` is the
"Total Assets" the Excel export carries. `JPS Status` includes **`Avoid`** — a
do-not-contact signal independent of the do-not-call list. `CATEGORY` (ID/Status)
carries "Barron's Top Advisor", "Masters Top 400", "Wells Top 400", "Gate
Keeper", "COI".

### Two structural answers

**Every one of the 190 fields has `isReadOnly = false`.** Writes are possible;
Phases 4 and 5 are feasible.

**No CRD field exists** anywhere in the 190 definitions — confirmed, not assumed.

### And a correction to the census reading

Several fields that looked like data-entry leakage are repurposed fields working
as designed. `HOME_COUNTRYNAME` displays as **"Partners"**, which is why one
record held "Katiebeth Worrell (daughter)" in what the JSON calls `country`.
`MOBILE_PHONE` displays as "Alt Phone 2"; `HOME_PHONE` as "Phone 2". The API
returns the internal column name, never the label, so the JSON is systematically
misleading about what these fields *are*. Nothing may be inferred from a field's
API name without checking its `displayName`.

---

## Phase 1 — Read from the API instead of the spreadsheet

**Value: unlocks the custom fields and removes a manual step. Risk: low.**

### The "EIC Contact" problem, and why it is no longer a blocker

The Excel column cannot be read through the API (see below). It does not need to
be: **territory assignment is derivable from state, more accurately than the
field records it.** The map lives in `territories.yaml`.

Seven territories, 51 of 51 states and DC, every one geographically contiguous:

| owner | states |
|---|---|
| SZ Steve Zimmerman | IA IL IN KY MI MN MO ND NE OH SD WI |
| SH Steve Halley | AK CA HI ID MT NV OR WA WY |
| TL Tate Lambeth | AR AZ CO KS LA NM OK TX UT |
| DM Dennis McKinney | CT MA ME NH NY RI VT |
| KT Keith Telesca | DC DE MD NJ PA VA WV |
| MK Matt Keeter | AL GA MS NC SC TN |
| SB Sam Borland | FL |

Every state resolved to exactly one owner with no ambiguity, and the map was
validated **out of sample**: built from non-LPL records only, then tested
against the 6,141 LPL contacts that had been assigned by hand — 6,140 matched,
100.0%.

It is more accurate than the CRM field because the field is blank on 9,223 LPL
contacts loaded in 2025 that nobody ever tagged. Each salesperson is responsible
for the LPL advisors in their territory whether or not the record says so, so
those blanks are a gap rather than a fact. The map also covers all 127,445
advisors instead of only the 47,426 in the CRM.

`JPP` (Paul Power, 36 states) and `RTI` (Terry Irrgang, 26) are national overlay
roles, not territories. Where the CRM names one of them on a specific contact,
that is a relationship fact and should win over the state rule.

### What the badge actually says

The field view currently renders "EIC relationship — owned by X. Coordinate
before you call," which reads as *a relationship exists here*. That is the wrong
reading of the field, and it is why a universal owner looked like a problem.

It is a **territory notice**: *"this advisor is in TN and is assigned to Matt
Keeter."* It tells a rep they are looking outside their own patch. Shown that
way it is rare from any one rep's point of view — most of what they look at is
their own territory — and it is silent for advisors they are responsible for.

That needs the signed-in user mapped to their initials; the app already resolves
identity through `/.auth/me`.

Relationship depth is a **separate** signal and deserves its own treatment:
`has_clients___eic` (4,080 current clients, 4,214 former), the `b*` book
($14.8B across 3,673 contacts), `lastReach`, `lastMeeting`. "Current client ·
$4.2M All-Cap · last met March" is worth more before a call than a set of
initials, and all of it comes through the API.

### The underlying API limitation, for the record

The Excel export's **"EIC Contact"** column is populated on **37,904 of 47,426
rows** with owner initials (SZ 8,128, TL 6,546, SH 5,690, DM 5,268, KT 5,066,
MK 3,595, SB 2,700, JPP 661). Those initials appear in **no field of the API
pull** — every one of the 225 keys across all 47,466 records was searched, and
there were three hits.

The metadata explains why and does not fix it. The system column `REFERREDBY`
carries `displayName = "EIC Contact"` with a picklist mapping the initials to
names (AW → Allen White, DM → Dennis McKinney, …). But the API's JSON property
`referredBy` returns the *custom* field `CUST_ReferredBy_094317334` instead —
885 records of free text like "Greg Hillard" and "Branch List 10.7.2010". The
system field is shadowed by the custom one and is not exposed.

**Tested and confirmed unreachable.** `$select=REFERREDBY` returns
`referredBy: null`, and naming both the custom and system columns together
returns the same. OData `$select` resolves to the same shadowed property, so the
field cannot be read through the API in any form tried.

Superseded by `territories.yaml` above. Copying `REFERREDBY` into a new custom
field inside Act! would also work — every one of the 190 fields is writable —
but it is now optional rather than blocking, and the derived map is the better
source regardless because it covers the 9,223 records the field never got.

### The build changes

| today | becomes |
|---|---|
| `Phone` | `businessPhone` **+ `businessExtension`** |
| `Total Assets` | `USER10` today; recompute from `ballcap_*` + `blarge_*` |
| `EIC Contact` | **`territories.yaml`, keyed on state** — not a CRM field at all |
| — | `id`, the stable Act! GUID |
| — | `has_clients___eic`, the `b*` book, `lastReach` / `lastMeeting` |
| — | `USER5` Program Type — the approved-platform code |

`businessExtension` matters out of proportion to its 394 records: the
`phone_kind` taxonomy treats `extension` as reaching a person, and CRM rows
currently arrive with `phone_kind` empty and get inferred.

**Do not** pick up `mobilePhone`. Its `displayName` is **"Alt Phone 2"** — the
field was repurposed and does not hold mobile numbers. Same for `HOME_PHONE`,
which displays as "Phone 2".

**Audit checks to add:**

- every column `load_crm()` reads exists in the source
- `territories.yaml` covers all 50 states and DC, no state in two territories,
  every owner code has a name — a territory silently losing its owner would put
  the wrong colleague's name in front of a rep
- where the CRM *does* record an owner and the state map disagrees, the count of
  disagreements stays near zero (2 of 6,290 today). A jump means the territory
  map has gone stale, which is the failure this file is most exposed to
- `bvalue == ballcap + blarge` disagreement rate does not grow

---

## Phase 2 — Use what the pull already contains

**Value: better data on the existing map. Risk: none, still no writes.**

1. **Per-strategy assets.** Replace the single blended figure with All-Cap and
   Large-Cap, computed from the per-account rows. This also bears on the
   team-assets problem `build_contacts.py` already documents — 623 shared asset
   values ascribed to every team member.
2. **Firm attribution from `user5`.** A second signal alongside the email
   domain. Where they disagree, that disagreement is itself worth reporting.
3. **`product_interest` as a filter.** Stated interest in All-Cap, Large-Cap or
   the fund, on 25,951 contacts.
4. **Drop the 26 `contactType = User` staff records** by rule.

---

## Phase 3 — Persist the CRD crosswalk

**Value: makes every later sync exact. Risk: none — a local file.**

`score_contacts()` already computes CRD ↔ CRM record every run, with a tier and
a score, and then discards the mapping. Persist it: CRD, Act! `id`, tier, score,
date. Current distribution is 26,412 high, 8,753 review, 12,261 no candidate.

**Audit check:** the high-tier mapping is stable between runs. A matcher change
that silently reassigns 400 people should surface as a number, not a surprise.

---

## Phase 4 — Write call outcomes back to Act!  (UNBLOCKED)

**Value: highest of anything here. Risk: real — this writes to the CRM.**
**Technically proven 2026-08-13.**

`POST /api/History` attaches a history record to a contact. The dialer currently
logs to Azure Table Storage, which the rest of the firm cannot see. This is the
answer to the question asked early in the project: *what problem does this solve
if it is not written to a CRM?*

### What the live test established

`src/act_write_test.py` posted one marked record to the author's own contact and
read it back. Confirmed:

- the write succeeds and returns the new id; a read-back verifies it landed
- `contacts: [{"id": ...}]` is the correct attachment shape
- **attribution works** — `createUserName` is the authenticated user, so a rep's
  logged calls appear in Act! as theirs
- `historyTypeID` is **optional**, and `DELETE /api/History/{id}` removes the row

### The type-0 trap

An untyped history record is silently stored as **`type=0`, "Call Attempted"** —
a claim that somebody phoned the advisor.

And `Call Attempted` *is* id `0`. So a falsy check (`if type_id:`) that drops the
field produces output identical to correctly sending `0`: the bug and the correct
behaviour are indistinguishable in the result. It cannot be caught by looking at
what was written.

**Therefore: always send `historyTypeID` explicitly, and test the boundary with
`is not None`.** The error direction is the safer one — a real "connected" would
understate as "attempted" — but it is still a false record in a CRM the whole
firm reads.

### Disposition → history type

Settled against the live CRM on **2026-08-14** by
`act_write_test.py --outcome-map`, which scheduled one activity per distinct
result, cleared each, and read back the type that actually landed. This matters
more than it sounds: the clear payload carries `result.id` *and* the task
carries `activityTypeId`, and until the probe ran we did not know which one
chose the history type. **It is the clear result.** Had it been `activityTypeId`
every outcome would have cleared to the same type, and a voicemail and a connect
would have been indistinguishable in the CRM — silently, with no error anywhere.

The eight buttons collapse onto Act!'s four Call results:

| dialer outcome | id | Act! type | also |
|---|---:|---|---|
| connected | 1 | Call Completed | |
| attempted | 0 | Call Attempted | replaced `no-answer`, absorbed `gatekeeper` |
| voicemail | 17 | Call Left Message | |
| callback | 0 | Call Attempted | + "Call back requested."; stays in the queue |
| received | 2 | Call Received | |
| wrong-number | 0 | Call Attempted | + "Wrong number."; flags the number bad |
| do-not-call | 1 | Call Completed | + "Asked not to be called again." |
| skipped | — | **nothing written** | no call was made |
| email sent | 16 | E-mail Sent | |

Three outcomes share result 0 and two share result 1, so each of those appends a
sentence to the history **details**. Without it a wrong number and an unanswered
ring are the same row in every Act! report, and the distinction would survive
only in our own database — which is exactly the kind of quiet loss this project
keeps producing.

`skipped` writes nothing: a skip is the absence of a call. `do-not-call` does
write, because the rep spoke to someone in order to be told.

The mapping lives in `Dial.OUTCOMES` in `webapp/dial.js`, beside the buttons,
rather than in the sync code — a collapse nobody reading the button list can see
is a collapse that will be re-invented. Audit check 31 holds `dial.js`,
`api/log/index.js` and `act_write_test.py` to the same vocabulary.

### Attribution — SOLVED, no vendor involvement needed

Proven on **2026-08-13** and re-confirmed across all four results on 2026-08-14.

**Do not write history directly.** Every direct route failed, and they were all
the same route wearing different hats: `POST /api/History` with
`recordManagerID` + `createUserID`, the same plus name strings, and
`PATCH /api/History/{id}` — which returned success and changed nothing. A search
of 254 endpoints found no impersonation or on-behalf-of parameter. Calling those
"four routes tested" overstated the coverage: they were four ways of asserting
attribution on a record we had already created.

**Write an activity instead.** Act! is activity-centric — the product creates
history as a *consequence* of clearing an activity, and the scheduling endpoint
takes a user id in the path:

```
POST /api/organizers/{userId}/tasks     scheduledForId = the rep
PUT  /api/tasks/{taskId}/clear          result.id = the outcome
```

The resulting history carries `recordManager` = **the rep**, with
`createUserName` = the authenticating account. That split is the right one:
the work is the rep's, and the record is honest about what wrote it.

Confirmed in the Act! UI: the Record Manager field on the resulting history
reads *Mr. Matt Keeter*.

**One caveat, unresolved.** The Act! UI's history header shows *"Created by …
Last edited by …"* — the **creator**, not the record manager, so a rep opening
the record sees the integration account's name at the top. Record Manager is
correct and is the field that drives ownership and reporting; but confirm the
entry appears in the rep's own history view and in activity reports before
relying on it. Also unverified: whether a **non-manager** Act! account may
schedule for a peer. Both are cheap to test and neither blocks building.

The earlier fallback — post from an integration account and name the rep in
`regarding` — is no longer needed and should not be built.

### Preconditions

Phase 3 (the crosswalk supplies the Act! contact id — **done**), a dry-run mode,
and only outcomes a rep explicitly logged — never an inferred one.

**Audit checks:** every outcome in `dial.js` maps to exactly one history type or
to nothing, with no silent default; the mapping is asserted with `is not None`
so `0` survives; nothing is posted for `do-not-call` or `skipped`.

---

## Phase 5 — The CRD field

**Value: plumbing. Risk: moderate. Deliberately last.**

Add a new custom field rather than repurposing one — `bcode10` and friends look
unused at 0.1% but are slots in a live account structure, not spare capacity.

Rules: machine-written and never hand-edited; only the 26,412 high-confidence
rows written, the rest left blank so presence means confidence; full idempotent
refresh each sync; snapshot before the first write.

The review tail of 8,753 is roughly twelve hours' work with a purpose-built
accept/reject screen and several weeks in a spreadsheet. The tooling decides
whether it happens. Do **not** work the 12,261 unmatched — most legitimately
have no CRD, being assistants, branch managers, wholesalers and vendors.

---

## Also available, not yet scheduled

- `GET /api/contacts/{id}/history` and `/notes` — prior-touch context for the
  advisor card, which today shows only our own call log.
- `GET /api/emarketing/analytics/campaign-results` / `campaign-sent` /
  `campaign-leads` — observed campaign behaviour, a better answer to "what do
  our blasts reach" than the hand-maintained `email__y_n` flag.
- `GET /api/contacts/by-ids` plus an OData filter on `edited` — incremental
  sync instead of 285 MB per run.
- Act!'s native `aemOptOut` / `aemBounceBack` are **unused** — `False` on all
  47,466 records, 2 of only 3 fields never populated anywhere in the schema. If
  the firm ever moves to a different email platform, the suppression list has to
  be reconstructed from `email__y_n` codes, because the native fields hold
  nothing.
> **Superseded production-source note (2026-08-26).** The historical
> `CRM_Contacts_*.xlsx` observations in this document describe an investigation
> snapshot, not a supported production input. The production identity path now
> consumes only the immutable `act_contacts_<pull-id>.json` plus
> `act_eic_contact_<pull-id>.json` pair committed by the matching
> `act_pull_manifest_<pull-id>.json`. The ledger verifies the newest
> manifest's filenames, completeness policy, byte counts, row counts, and
> SHA-256 hashes, and refuses a newer orphan contacts file. Do not restore the
> stale CRM Excel export as a fallback.
>
> A CRD typed into ACT is an assertion, not proof. Automatic approval requires
> an independent SEC/roster corroborator; comparable current firm or address
> contradictions are review cases. Human decisions remain evidence-hash bound.
>
