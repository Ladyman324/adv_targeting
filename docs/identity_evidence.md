# Identity evidence inventory — Act! contact ↔ SEC advisor CRD

**What this is.** A raw-material census, not a matching rule. It answers one
question per field: *if you were deciding whether an Act! contact and a CRD are
the same person, could this field help, and how often is it actually there?*

Every number below was produced by loading the file named beside it. Nothing is
estimated. Measurement scripts were run from the repo root against:

| world | file | rows |
|---|---|---:|
| A | `data/raw/act_contacts_2026-08-13.json` (newest pull) | 47,466 |
| B | `data/interim/advisors.parquet` | 436,091 |
| B | `data/interim/advisor_branches.parquet` | 686,187 |
| B | `data/interim/advisor_employments.parquet` | 444,095 |
| B | `data/interim/advisor_employment_history.parquet` | 1,938,188 |
| B | `data/interim/advisor_exams.parquet` | 632,240 |
| B | `data/interim/advisor_prior_registrations.parquet` | 511,214 |
| B | `data/output/firms.parquet` | 16,935 |
| C | `webapp/data/contacts.json` → `advisors` | 136,395 |
| C | `data/output/advisor_emails.json.gz` (built 2026-08-22 from contacts.json) | 123,169 addressable / 122,813 resolved |
| — | `data/interim/act_crosswalk.parquet` | 47,466 (**41,594 matched**: 26,725 `high` + 14,869 `review`; 5,872 `none`) |

`webapp/data/contacts_0..3.json` and `contacts_base.json` are shards of the same
`advisors` map in `contacts.json` (`contacts_base` carries `teams`, `practices`
and the shard manifest, and an empty `advisors`). They add no fields, so they are
not censused separately.

---

## 0. Read this before trusting anything downstream

**The 41,594 matched rows are not a neutral sample.** `src/act_crosswalk.py`
calls `score_contacts()` in `src/build_contacts.py`, which already consumes:

- **surname** — a hard gate (`index.get(last, …)`); a row cannot match at all
  without a filed-surname hit;
- **given name** via `name_score()` over SEC name forms;
- **firm CRD derived from the Act! email domain** (`derive_domain_map()`,
  `act_crosswalk.py:86`) — weight `W_FIRM`;
- **city** and **state** from `businessAddress` — weights `W_CITY`, `W_STATE`;
- **suffix**;
- **the email local part's surname half only**, as a nickname fallback
  (`build_contacts.py:1006-1024`). The comment there is explicit that scoring the
  local part's *given* half against the SEC forms was tried and abandoned.

So surname, given name, email **domain**, city and state agreement rates measured
on the matched set are inflated by construction. Street, ZIP, phone, job title,
middle name, website and the email local part's **given** half are *not* consumed
by the current matcher and their rates below are informative.

**There is no CRD anywhere in Act!.** All 47,466 `customFields` were scanned for
4–7-digit values; the highest hit was `user2` (90 numeric of 317 populated,
values like `'1'`, `'2'`). No field holds a registration number. There is no
direct key; every signal in this document is circumstantial.

---

## 1. World A — Act! CRM (`data/raw/act_contacts_2026-08-13.json`, 47,466 rows)

Coverage = non-empty share of 47,466. "Distinct" is distinct non-empty values.

### Name evidence

| field | holds | non-empty | % | distinct | example |
|---|---|---:|---:|---:|---|
| `id` | Act! GUID, durable across pulls | 47,466 | 100.0% | 47,466 | `75c932e2-60db-4bb7-b5be-00007c67223e` |
| `firstName` | given name as typed by a rep | 47,465 | 100.0% | 4,221 | `Bruce` |
| `lastName` | surname | 47,465 | 100.0% | 23,086 | `Melton` |
| `fullName` | prefix + first + last concatenated | 47,465 | 100.0% | 46,756 | `Mr. Bruce Melton` |
| `middleName` | middle name or initial | 8,999 | **19.0%** | 844 | `B.` |
| `salutation` | how the rep addresses them — the nickname store | 47,419 | 99.9% | 4,336 | `Bruce` |
| `namePrefix` | Mr./Ms./Dr. | 29,942 | 63.1% | 19 | `Mr.` |
| `nameSuffix` | Jr/III | 2,194 | 4.6% | 63 | `Jr` |

### Contact evidence

| field | holds | non-empty | % | distinct | example |
|---|---|---:|---:|---:|---|
| `emailAddress` | primary business email | 41,868 | 88.2% | 40,749 | `bruce.melton@benjaminfedwards.com` |
| `emailAddress` (well-formed only) | has `@` and a dotted domain | **40,786** | **85.9%** | — | — |
| `businessPhone` | office line, digits only | 30,628 | 64.5% | 24,384 | `9038938338` |
| `alternatePhone` | second line | 12,899 | 27.2% | 7,306 | `(415) 576-2125` |
| `faxPhone` | fax | 12,782 | 26.9% | 6,154 | `2162410835` |
| `mobilePhone` | cell | 456 | **1.0%** | 363 | `9017616310` |
| `homePhone` | home line | 231 | **0.5%** | 140 | `2025855414` |
| `altEmailAddress` | second email | 50 | **0.1%** | 50 | `Joyce.bussie@lindenthomas.com` |
| `personalEmailAddress` | third email slot | 50 | **0.1%** | 50 | byte-identical to `altEmailAddress` in all 50 rows |
| `website` | advisor's page or firm site | 32,955 | 69.4% | 21,959 | `https://www.benjaminfedwards.com/branch-employee/bruce-melto…` |

**`emailAddress` is not always an email.** 1,082 of the 41,868 populated values
(2.6%) have no `@` or no dotted domain — reps use the field as a status note:
`retired`, `unsubscribed 7/11/25`, `not currently registered`, `request
unsubscribe 1/4/23`. Any consumer must validate the shape first.

**Email is near-unique, phone is not.** 40,749 distinct addresses over 41,868
rows; only 278 addresses appear on more than one contact (1,397 rows involved).
`businessPhone` is far weaker: 23,642 distinct 10-digit numbers over 30,603
usable rows, and 9,942 rows share their number with at least one other contact —
`2123281000` alone appears 66 times. It is a switchboard, not a person.

### Address evidence

| field | holds | non-empty | % | distinct | example |
|---|---|---:|---:|---:|---|
| `businessAddress.line1` | street | 46,731 | 98.5% | 19,205 | `1303 N Travis St` |
| `businessAddress.line2` | suite/floor | 24,842 | 52.3% | 2,211 | `Suite 201` |
| `businessAddress.city` | city (contains typos — `Sheman`) | 47,186 | 99.4% | 3,313 | `Sheman` |
| `businessAddress.state` | state; **65 distinct values for 50 states** | 47,213 | 99.5% | 65 | `TX` |
| `businessAddress.postalCode` | ZIP | 37,522 | 79.1% | 4,798 | `75092` |
| `businessAddress.latitude` / `.longitude` | geocode, derived not filed | 35,712 | 75.2% | 4,023 / 4,054 | `33.6372` / `-96.6184` |
| `businessAddress.country` | mostly garbage (`B`) | 4,847 | 10.2% | 17 | `B` |
| `homeAddress.line1` | free-text notes, not an address | 1,041 | **2.2%** | 668 | `no longer at GV` |
| `homeAddress.city` / `.state` / `.postalCode` | home address | 142 / 120 / 124 | **0.3% / 0.25% / 0.26%** | 90 / 29 / 92 | `Plantation` / `FL` / `33324-4468` |

### Affiliation and role evidence

| field | holds | non-empty | % | distinct | example |
|---|---|---:|---:|---:|---|
| `company` | firm name, free text | 31,865 | 67.1% | 1,958 | `Benjamin F. Edwards` |
| `jobTitle` | title | 29,279 | 61.7% | 2,491 | `Managing Director` |
| `department` | branch/office label | 15,369 | 32.4% | 7,772 | `Sherman Branch` |
| `custom.user5` | internal firm code | 31,415 | 66.2% | 31 | `OSC` |
| `custom.area` | internal territory code | 16,682 | 35.1% | 986 | `NoCA - SAN FRANCISCO` |
| `custom.position` | role | 4,372 | **9.2%** | 99 | `Advisor` |
| `custom.branch_name` | branch label | 1,146 | **2.4%** | 121 | `New York PWM` |
| `custom.branch_id_1` | branch label + internal code | 1,146 | **2.4%** | 140 | `New York PWM - '690` |
| `custom.assistants_name` | assistant name **or their email** | 5,410 | 11.4% | 4,313 | `eric.froistad@ubs.com` |
| `custom.email_2_email` | a second email address | 1,333 | **2.8%** | 1,114 | `regina.gerlich@wellsfargoadvisors.com` |
| `custom.comments` | free-text notes | 13,229 | 27.9% | 7,244 | `Lead from Sam Sneed Paris, TX` |
| `referredBy` | free text, not a referrer id | 885 | **1.9%** | 454 | `Barron's # 3 Producer in WA` |
| `birthday` | — | **0** | **0.0%** | 0 | — |
| `recordManager` | EIC rep who owns the record | 47,466 | 100.0% | 26 | `Ms. Erin Berger` |
| `created` | record creation timestamp | 47,466 | 100.0% | 29,787 | `2025-04-04T13:16:54+00:00` |

**Independence.** World A is the reference side. Every field here is independent
of the SEC (world B) — it was typed or imported by an EIC rep, never sourced from
a filing. It is *not* independent of world C: 34,962 of the 136,395 `contacts.json`
advisors carry `src: "CRM"`, i.e. they are this same Act! data re-shaped.

---

## 2. World B — SEC / FINRA

### `advisors.parquet` — 436,091 rows, one per CRD

| field | holds | non-empty | % | distinct | example |
|---|---|---:|---:|---:|---|
| `advisor_crd` | the identifier being matched to | 436,091 | 100.0% | 436,091 | `2752004` |
| `first_name` | filed legal given name, upper-case | 436,091 | 100.0% | 32,973 | `LAWRANCE` |
| `last_name` | filed surname | 436,091 | 100.0% | 149,668 | `JOHNSON` |
| `middle_name` | filed middle name or initial | 352,503 | 80.8% | 33,959 | `MALONE` |
| `used_first_name` | the nickname the filing records | 77,403 | **17.8%** | 11,056 | `BILL` |
| `suffix` | Jr/III | 20,019 | **4.6%** | 104 | `III` |
| `designations` | CFP/ChFC etc. | 83,634 | 19.2% | 16 | `Chartered Financial Consultant` |
| `iapd_url` | public profile link | 436,091 | 100.0% | 436,091 | `https://adviserinfo.sec.gov/individual/summary/275…` |
| `n_exams`, `n_prior_firms`, `n_other_business` | counts | 436,091 | 100.0% | 6 / 20 / 2 | `2` |
| `has*` disclosure flags (10 cols) | booleans | 436,091 | 100.0% | 2 | `False` |

**There is no person-level email or phone anywhere in the SEC data.** Confirmed
by column scan: `advisors.parquet` has no `email` column, and no other world-B
table carries one. Every advisor email in this project comes from a scraped firm
roster or from Act!, never from a filing. This is the single most important fact
in the inventory.

### `advisor_branches.parquet` — 686,187 rows, 435,171 distinct CRDs

| field | holds | non-empty | % of rows | distinct | example |
|---|---|---:|---:|---:|---|
| `advisor_crd` | person | 686,187 | 100.0% | 435,171 | `2752004` |
| `firm_crd` | employer | 686,187 | 100.0% | 26,516 | `283330` |
| `branch_street1` | filed office street | 551,117 | 80.3% | 146,954 | `5707 SOUTHWEST PARKWAY` |
| `branch_street2` | suite | 265,288 | 38.7% | 15,406 | `BUILDING 2, SUITE 400` |
| `branch_city` | city | 686,187 | 100.0% | 20,974 | `AUSTIN` |
| `branch_state` | state | 686,187 | 100.0% | 53 | `TX` |
| `branch_postal` | ZIP | 551,117 | 80.3% | 23,474 | `78735` |
| `branch_country` | country | 686,024 | 99.98% | 5 | `United States` |

1.58 branches per advisor on average (median 1, p90 2, max 382). **Discriminative
power:** 137,367 distinct `street1 + ZIP5` keys; 54.6% of keys map to exactly one
CRD, mean 3.96 CRDs per key. `city + state` is much weaker — 27,337 keys, mean
23.8 CRDs, median 3.

### `advisor_employments.parquet` — 444,095 rows, all 436,091 CRDs

| field | holds | non-empty | % | distinct | example |
|---|---|---:|---:|---:|---|
| `firm_crd` / `firm_name_on_record` | current employer | 444,095 | 100.0% | 26,653 / 26,473 | `KESTRA ADVISORY SERVICES, LLC` |
| `emp_street1` | **the firm's main office, not the person's desk** | 432,113 | 97.3% | 16,052 | `5707 SOUTHWEST PARKWAY` |
| `emp_city` / `emp_state` / `emp_postal` | firm main office | 444,095 / 443,280 / 432,106 | 100% / 99.8% / 97.3% | 3,991 / 53 / 5,942 | `AUSTIN` / `TX` / `78735` |
| `branch_city` / `branch_state` | rolled-up branch location | 444,027 / 442,837 | 99.98% / 99.7% | 16,169 / 53 | `AUSTIN` |
| `reg_earliest_date` | first registration at this firm | 444,095 | 100.0% | 10,007 | `2023-05-08` |
| `reg_states` | pipe-joined state list | 444,095 | 100.0% | 16,066 | `CA\|TX` |
| `n_branch_locations`, `n_registrations` | counts | 444,095 | 100.0% | 72 / 52 | `2` |

Only 16,052 distinct `emp_street1` values across 432,113 rows — it is a *firm*
address repeated on every employee. It is near-useless as person evidence, and
the cross-world numbers in §3 confirm that.

### `advisor_employment_history.parquet` — 1,938,188 rows, 435,935 CRDs

| field | non-empty | % | distinct | example |
|---|---:|---:|---:|---|
| `firm_name_on_record` | 1,938,188 | 100.0% | 471,935 | `WELLS FARGO ADVISORS, LLC` |
| `city` | 1,938,188 | 100.0% | 40,436 | `WALNUT CREEK` |
| `state` | 1,920,393 | 99.1% | 55 | `CA` |
| `from_date` | 1,938,187 | 100.0% | 754 | `07/2014` |
| `to_date` | 1,164,852 | 60.1% | 120 | `11/2016` |

Historic city/firm pairs. Relevant only for stale Act! records; Act! has no
employment-history structure to join against (see §5).

### `advisor_exams.parquet` — 632,240 rows, 424,850 CRDs

`exam_code` (3 distinct: e.g. `S63`), `exam_name`, `exam_date` (15,049 distinct,
100% populated). **Zero joinability** — Act! records no exam, licence or
registration date for anyone. Listed only to close the question.

### `advisor_prior_registrations.parquet` — 511,214 rows, 242,538 CRDs (55.6% of advisors)

`firm_crd` (21,538 distinct), `firm_name_on_record`, `reg_begin`, `reg_end`
(both 511,208 = 100.0%). Same problem: Act! `company` is a single current-firm
string with no dates.

### `firms.parquet` — 16,935 rows (firm-level, not person-level)

| field | non-empty | % | distinct | example |
|---|---:|---:|---:|---|
| `crd` | 16,935 | 100.0% | 16,935 | `175112` |
| `name` / `legal_name` | 16,935 | 100.0% | 16,869 / 16,893 | `CAPTRUST` / `CAPFINANCIAL PARTNERS, LLC` |
| `phone` | 16,932 | 99.98% | 16,211 | `919-870-6822` |
| `website_as_filed` | 15,587 | 92.0% | 15,309 | `HTTPS://WWW.LINKEDIN.COM/COMPANY/CAPTRUST` |
| `Main Office Street Address 1` | 15,577 | 92.0% | 12,547 | `4208 SIX FORKS RD` |
| `city` / `state` / `postal` | 15,577 / 14,690 / 15,488 | 92.0% / 86.7% / 91.5% | 2,101 / 53 / 4,999 | `RALEIGH` / `NC` / `27609` |

`website_as_filed` is often a LinkedIn or Facebook URL rather than a domain — the
example above is the actual first non-empty value.

**Independence.** All of world B is filed with the SEC and is fully independent
of Act!. It is also independent of world C's *identity* claims but **not** of
world C's CRD assignment — `contacts.json` was built by matching rosters to these
same CRDs.

---

## 3. World C — built contacts (`webapp/data/contacts.json`)

136,395 advisors, keyed by CRD. Coverage is share of 136,395.

| key | holds | non-empty | % | example |
|---|---|---:|---:|---|
| `n` | display name | 136,395 | 100.0% | `RICHARD  BLYTHE` |
| `src` | **provenance** | 136,395 | 100.0% | `Cetera` |
| `t` | match tier (`confirmed`/`high`/`review`) | 136,395 | 100.0% | `confirmed` |
| `ms` | match score | 136,395 | 100.0% | `1.0` |
| `cs` | contact state | 136,047 | 99.7% | `AZ` |
| `wk` | phone kind (`direct`/`switchboard`) | 132,592 | 97.2% | `direct` |
| `w` / `wd` | work phone E.164 / display | 132,572 / 132,573 | 97.2% | `+15208823192` / `(520) 882-3192` |
| `fc` | firm CRD | 130,247 | 95.5% | `105644` |
| `cn` | firm name | 123,666 | 90.7% | `Cetera` |
| **`e`** | **email** | **123,188** | **90.3%** | `julian.pace@wellsfargoadvisors.com` |
| `pu` | public profile URL | 88,822 | 65.1% | `https://fa.wellsfargoadvisors.com/julian-pace/` |
| `ti` | job title | 84,630 | 62.1% | `Financial Advisor` |
| `tn` | team name | 41,832 | 30.7% | `David Moss` |
| `pk` | practice key | 37,652 | 27.6% | `8174\|berlincarter…` |
| `also` | alternate firm names | 31,627 | 23.2% | `['Janney Montgomery Scott']` |
| `o` | owning EIC rep initials | 27,128 | 19.9% | `DM` |
| `wf` | phone-source firm | 16,254 | 11.9% | `Janney Montgomery Scott` |
| `li` | LinkedIn URL | 16,061 | 11.8% | `https://www.linkedin.com/in/davidmossadvisor` |
| `ia` | individual assets | 1,874 | **1.4%** | `193771.91` |
| `tm` | team key | 1,096 | **0.8%** | `ubsfinancialservicesinc\|atlanta\|193027` |
| `c` / `cd` | alternate phone | 293 | **0.2%** | `+18008280717` |
| `wx` | phone extension | 23 | **0.02%** | `99012` |

Also in the file: `teams` (572 entries) and `practices` (9,812 entries), each a
key → `{name, members[[crd, state]], size}`. Membership is CRD-keyed and carries
no independent identity evidence about an Act! contact.

### The provenance split — this is what makes world C usable or not

`src` distribution over 136,395 advisors:

| src | advisors | with email `e` |
|---|---:|---:|
| **CRM (= Act!)** | **34,962** | **32,353** |
| Edward Jones | 19,266 | 19,110 |
| Merrill Lynch | 14,735 | 14,735 |
| Ameriprise | 9,115 | 9,115 |
| LPL Financial | 8,924 | 8,774 |
| Morgan Stanley | 8,897 | 8,895 |
| Cetera | 8,666 | 53 |
| Wells Fargo Advisors | 6,032 | 6,022 |
| Raymond James (+ branch pages, Alex. Brown) | 7,611 | 7,611 |
| UBS | 5,419 | 5,336 |
| Northwestern Mutual | 3,937 | 3,937 |
| 19 other rosters | 8,831 | 7,246 |
| **total non-CRM with email** | — | **90,835** |

**26.3% of the emails in `contacts.json` are Act!'s own emails.** For those
34,962 CRDs, `e`, `w`, `ti` and `cs` are Act! data wearing a CRD, and agreeing
with Act! proves nothing. Only the 90,835 roster-sourced emails are independent
evidence.

### `data/output/advisor_emails.json.gz`

Built 2026-08-22 by `src/export_advisor_emails.py` **from `contacts.json`**
(`source: "contacts.json"`, confirmed in the file's own metadata).

| key | contents | size |
|---|---|---:|
| `byCrd` | CRD → email | 123,169 |
| `byEmail` | email → CRD | 122,813 |
| `byDomain` | domain → firm CRD | 385 |
| `ambiguous` | addresses mapping to >1 CRD (e.g. `johnsonc@stifel.com`) | 172 |
| `internalCrds` | EIC's own CRDs | 18 |

**Independence: inherited, not new.** This file adds no observation; it is a
projection of `contacts.json.e`. Every `byCrd` entry whose CRD has `src: "CRM"`
is Act!-derived and is *not* independent evidence. The file does not carry `src`,
so a consumer must join back to `contacts.json` to know which side of the line an
address falls on.

---

## 4. Cross-world joinability on the 41,594 matched crosswalk rows

All rows from `data/interim/act_crosswalk.parquet` where `tier ∈ {high, review}`,
joined to Act! by `act_id` and to worlds B/C by `advisor_crd`. Every one of the
41,594 CRDs resolves in `advisors.parquet`; 41,583 (99.97%) resolve in
`contacts.json`; 38,682 (93.0%) have an entry in `advisor_emails.byCrd`.

"Both" = the count of matched rows where **both** sides are populated. That
number is the ceiling on how often the signal can be consulted at all.

| Act! field | other-world field | A side | B/C side | **both** | both % | agreement among both |
|---|---|---:|---:|---:|---:|---|
| `emailAddress` | `advisor_emails.byCrd` (C) | 37,919 | 38,682 | **37,402** | **89.9%** | 32,433 exact (86.7%) |
| `emailAddress` (well-formed) | `advisor_emails.byCrd` (C) | 37,249 | 38,682 | **37,033** | **89.0%** | — |
| ↳ *of which C side is `src: CRM`* | — | — | — | **33,682** | 81.0% | **not independent** |
| ↳ *of which C side is roster-sourced* | — | — | — | **3,720** | **8.9%** | 877 exact (23.6%) |
| `businessPhone` | `contacts.json.w` (C) | 25,969 | 40,767 | **25,857** | **62.2%** | 19,040 last-10 (73.6%) |
| ↳ *roster-sourced C side only* | — | — | — | **3,228** | **7.8%** | 653 (20.2%) |
| `businessPhone` | `firms.phone` (B, firm main office) | 25,969 | 41,589 | 25,966 | 62.4% | **486 (1.9%)** |
| `businessAddress.line1` | `advisor_branches.branch_street1` (B) | 41,100 | 39,725 | **39,249** | **94.4%** | 18,018 normalised (45.9%); 22,412 house-number-only (57.1%) |
| `businessAddress.city` | `advisor_branches.branch_city` (B) | 41,426 | 41,594 | **41,426** | **99.6%** | 26,653 (64.3%) |
| `businessAddress.state` | `advisor_branches.branch_state` (B) | 41,443 | 41,594 | **41,443** | **99.6%** | 35,845 (86.5%) |
| `businessAddress.postalCode` | `advisor_branches.branch_postal` (B) | 31,864 | 39,725 | **30,959** | **74.4%** | 19,472 ZIP5 (62.9%) |
| `businessAddress.line1` | `advisor_employments.emp_street1` (B) | 41,100 | 41,525 | 41,034 | 98.7% | **969 (2.4%)** |
| `businessAddress.postalCode` | `advisor_employments.emp_postal` (B) | 31,864 | 41,525 | 31,814 | 76.5% | **1,426 (4.5%)** |
| `company` | `advisor_employments.firm_name_on_record` (B) | 26,947 | 41,594 | **26,947** | **64.8%** | 13,867 containment (51.5%) |
| `website` | `firms.website_as_filed` (B) | 29,852 | 41,421 | 29,768 | 71.6% | **1,690 host-equal (5.7%)** |
| `website` | `contacts.json.pu` (C) | 29,852 | 15,885 | 13,470 | **32.4%** | 1,604 exact (11.9%) |
| `emailAddress` domain | `firms.website_as_filed` host (B) | 37,919 | 41,421 | 37,805 | 90.9% | **4,616 (12.2%)** |
| `jobTitle` | `contacts.json.ti` (C) | 25,094 | 25,562 | **24,587** | **59.1%** | 21,699 (88.3%) — but see note |
| `middleName` | `advisors.middle_name` (B) | 7,619 | 37,231 | **7,133** | **17.2%** | 5,363 initial (75.2%); 1,511 full (21.2%) |
| `firstName` | `advisors.first_name` (B) | 41,594 | 41,594 | 41,594 | 100.0% | 31,847 (76.6%) — *consumed by matcher* |
| `firstName` | `advisors.used_first_name` (B) | 41,594 | 8,525 | **8,525** | **20.5%** | 1,861 (21.8%) |
| `lastName` | `advisors.last_name` (B) | 41,594 | 41,594 | 41,594 | 100.0% | 41,417 (99.6%) — *hard gate, meaningless* |

### How to read the important lines

- **Email is the strongest signal that exists, and 90% of its apparent strength
  is a mirror.** 37,402 matched rows have email on both sides — but 33,682 of
  those (90.1% of the pairs) have a C-side address that *came from Act!*.
  Genuinely independent email-vs-email comparison is available on **3,720 rows,
  8.9% of the matched set**, and there the two sides agree only 23.6% of the
  time. Independent email is a *rare* signal, not a common one.
- **Phone has no SEC counterpart at all.** The only person-level phone in the
  project is `contacts.json.w`, and the independent (roster-sourced) subset is
  3,228 rows — **7.8%**. Matching Act! `businessPhone` to `firms.phone` is
  effectively noise: 1.9% agreement on 25,966 comparable rows, because
  `firms.phone` is one switchboard for the whole firm.
- **`advisor_branches` is the address table to use; `advisor_employments` is
  not.** Same Act! street compared against branch street agrees 45.9% of the
  time on 39,249 comparable rows; against `emp_street1` it agrees **2.4%** on
  41,034 rows. `emp_street1` has only 16,052 distinct values over 432k rows — it
  is the firm's headquarters stamped on every employee.
- **The house number carries most of the address signal.** Full normalised
  street agrees 45.9%; leading house number alone agrees 57.1%. The 11-point gap
  is suite/abbreviation formatting, not different buildings.
- **`jobTitle` ↔ `ti` looks excellent (88.3%) and is worthless.** 21,775 of the
  24,587 comparable rows are `src: CRM`, so Act! is being compared with itself.
- **`middleName` is high-quality and rare.** Where both sides have one (7,133
  rows, 17.2%), the middle *initial* agrees 75.2%. The full middle name agrees
  only 21.2% because Act! stores `B.` and the SEC stores `MALONE`.
- **Neither website nor email domain identifies the firm reliably against
  filings.** Email domain matches the filed website host on 12.2% of 37,805
  comparable rows. The project's own `derive_domain_map()` exists precisely
  because `website_as_filed` cannot be used this way.

---

## 5. The email local part as a name signal

Population: the **37,919** matched rows where Act! has a non-empty
`emailAddress` (91.2% of 41,594). Local part = text before `@`, lower-cased,
non-alphanumerics stripped. "Contains" = the normalised name (≥2 letters) occurs
as a substring.

### Single-signal presence

| test | rows | % of 37,919 |
|---|---:|---:|
| local part contains **Act `firstName`** | 27,616 | 72.8% |
| local part contains **SEC `first_name`** | 22,525 | 59.4% |
| local part contains **SEC `middle_name`** | 1,388 | 3.7% |
| local part contains **SEC `used_first_name`** | 3,769 | 9.9% |
| local part contains **Act `lastName`** | 36,235 | **95.6%** |
| *(SEC `middle_name` populated at all)* | 28,874 | 76.2% |

### Requested distribution over {Act first, SEC first, SEC middle}

| bucket | rows | % of 37,919 |
|---|---:|---:|
| **Act first + SEC first** (both) | 22,032 | **58.1%** |
| **neither** (none of the three) | 9,429 | **24.9%** |
| **Act first only** | 4,641 | 12.2% |
| Act first + SEC middle | 881 | 2.3% |
| **SEC middle only** | 443 | 1.2% |
| **SEC first only** | 429 | 1.1% |
| all three | 62 | 0.2% |
| SEC first + SEC middle | 2 | 0.005% |

Collapsed to the two given names only:

| | SEC first absent | SEC first present |
|---|---:|---:|
| **Act first absent** | 9,872 | 431 |
| **Act first present** | 5,522 | 22,094 |

Where Act! and the SEC disagree on the given name outright (8,333 rows, 22.0% of
the email population), the local part sides with **Act! 5,865 times**, with the
SEC first name 774 times, with the SEC middle name 945 times, and with none of
them 1,978 times. The firm's own email spells the name the CRM has, not the name
the filing has — which is what makes the local part a real signal rather than a
restatement of `advisors.first_name`.

### The "neither" case is a naming *scheme*, not a mismatch

9,429 rows (24.9%). Of these:

- **8,493 (90.1%) still contain the Act! surname.**
- **6,011 of the 9,429 (63.7%) start with the Act! first initial.** Across all
  rows whose local part lacks the Act! first name, 6,393 of 10,303 (62.1%) start
  with its initial.
- only 70 have a local part of ≤3 characters.
- tier split: 6,387 `high`, 3,042 `review` — the bucket is not concentrated in
  weak matches.
- top domains: `lpl.com` (3,528), `stifel.com` (1,833), `raymondjames.com` (303),
  `ubs.com` (258), `wellsfargoadvisors.com` (202), `rbc.com` (198),
  `morganstanley.com` (176), `ml.com` (165), `janney.com` (165). Stifel's
  `surname + initial` and LPL's truncations dominate.

**15 examples of "neither"** (random sample, `random_state=7`, from the joined
frame):

| Act! email | Act! name | SEC name (first / middle / last) | CRD | tier |
|---|---|---|---|---|
| `matt.ponder@lpl.com` | Matthew Ponder | MATTHEW / C / PONDER | 5249751 | high |
| `kvogelsang@wfafinet.com` | Karen Vogelsang | Karen / Virginia / Vogelsang | 1347031 | review |
| `jef.lockman@raymondjames.com` | Jeffery Lockman | JEFFERY / W / LOCKMAN | 2354538 | review |
| `sosborne@rwbaird.com` | Scott Osborne | DUSTIN / SCOTT / OSBORNE | 5707804 | review |
| `colej@stifel.com` | Jeffrey Cole | JEFFREY / HOWARD / COLE | 2916796 | high |
| `jeff.dieringer@lpl.com` | Jeffrey Dieringer | JEFFREY / ALAN / DIERINGER | 4131962 | high |
| `dnardi@cerity.com` | Douglas Nardi | DOUGLAS / CARL / NARDI | 1511007 | high |
| `unsubscribed 3/18/26` | Thomas Schmidt | BRADLEY / THOMAS / SCHMIDT | 5810011 | review |
| `shimerc@stifel.com` | Chip Shimer | CHARLES / CRAWFORD / SHIMER | 2147235 | high |
| `sagricola@nbcsecurities.com` | Stephen Agricola | STEPHEN / TAYLOR / AGRICOLA | 5419446 | review |
| `ainfante@stifel.com` | Ana Infante | ANA / V / INFANTE | 2909784 | high |
| `chris.helin@lpl.com` | Christopher Helin | CHRISTOPHER / EERO / HELIN | 4557954 | high |
| `tom.halstenson@lpl.com` | Thomas Halstenson | JON / THOMAS / HALSTENSON | 5141044 | review |
| `chris.weng@raymondjames.com` | Christopher Weng | CHRISTOPHER / S / WENG | 2413401 | high |
| `bdawson@bdfwealth.com` | Brian Dawson | BRIAN / DOUGLAS / DAWSON | 4096605 | high |

What the sample shows, in the order it matters:

1. **Truncated forenames** — `matt.` for Matthew, `jeff.` for Jeffrey, `chris.`
   for Christopher, `tom.` for Thomas, `jef.` for Jeffery. Substring containment
   fails on all of them; a prefix test would catch every one.
2. **Initial + surname** — `kvogelsang`, `dnardi`, `ainfante`, `bdawson`,
   `sosborne`.
3. **Surname + initial** (Stifel) — `colej`, `shimerc`.
4. **Genuine nicknames** — `shimerc` is Act!'s *Chip* for SEC's *Charles*;
   neither name is in the local part, so nothing about it reaches the email at all.
5. **Middle-name-as-known-name** — `sosborne` (Dustin **Scott** Osborne),
   `tom.halstenson` (Jon **Thomas** Halstenson). The email agrees with the CRM's
   given name and both are the SEC *middle* name.
6. **The malformed-email leak** — `unsubscribed 3/18/26` is in the bucket because
   it is a status note in the email column. 1,082 such values exist repo-wide
   (§1); they must be filtered before any local-part test runs.

A deterministic slice, the 15 alphabetically-first "neither" emails, is in the
measurement output and shows the same shapes (`AAlexander@rockco.com`,
`Adamkiewiczj@stifel.com`, `AJ.Jordan@wellsfargoadvisors.com`, and
`A_burgos@ml.com` — Act! *Adrian* Burgos vs SEC *JORGE LUIS* Burgos, the one
example in either sample where the two records may genuinely be different people).

---

## 6. Fields that look promising but are too sparse to rely on

Ranked by how tempting they are versus what they actually cover.

| field | source | coverage | verdict |
|---|---|---|---|
| **Independent (roster) email vs Act! email** | C `contacts.json.e` where `src ≠ CRM` | **3,720 / 41,594 matched = 8.9%** | The single best evidence type in the project, available on fewer than one row in eleven. Usable as a *confirmer*, never as a gate. |
| **Any person-level phone in the SEC data** | — | **0** | Does not exist. Stop looking. |
| **Independent phone vs Act! `businessPhone`** | C `contacts.json.w` where `src ≠ CRM` | **3,228 = 7.8%** | Same ceiling as above, and 32.4% of Act! rows have no phone to begin with. |
| `middleName` | A | 19.0% overall; **17.2% both-populated** | Initial agrees 75.2% where present. Good quality, one row in six. Cannot be required. |
| `used_first_name` | B | 17.8% of CRDs; **20.5% both-populated** | The obvious nickname fix, and it is absent for four CRDs in five. The email local part covers nicknames far more often (§5). |
| `businessAddress.postalCode` | A | 79.1% (74.4% both-populated vs branches) | Usable, but one Act! row in five has no ZIP at all — a rule that needs ZIP silently abstains on 9,700 matched rows. |
| `company` | A | 67.1%; **14,647 matched rows blank** | A third of the CRM has no firm name. The email domain already carries this and covers 88%. |
| `website` | A | 69.4%, but agrees with `firms.website_as_filed` on **5.7%** | High coverage, near-zero signal. Act! stores advisor bio pages; the SEC stores corporate or LinkedIn URLs. |
| `custom.email_2_email` | A | **2.8%** (1,333 rows) | A real second email that no pipeline reads. Too sparse to build on; worth harvesting into the email pool. |
| `custom.assistants_name` | A | 11.4%, and holds *the assistant's* email/name | Evidence about a different person. Actively dangerous if fed to a matcher. |
| `custom.branch_id_1` / `branch_name` | A | **2.4%** | Would join beautifully to `advisor_branches` if it existed on more than 1,146 records. |
| `custom.position` | A | **9.2%** | `jobTitle` already covers 61.7%. |
| `altEmailAddress` / `personalEmailAddress` | A | **0.1%** (50 rows, byte-identical to each other) | Effectively empty. Two schema slots, one duplicated value, 50 people. |
| `mobilePhone` / `homePhone` | A | **1.0% / 0.5%** | Empty. |
| `homeAddress.*` | A | **0.3%**, and `line1` holds free text (`no longer at GV`) | Not an address field. |
| `birthday` | A | **0.0%** | Zero rows. The one field that would be near-decisive if it existed on either side, and it exists on neither. |
| `advisor_exams.*` | B | 100% of 424,850 CRDs | No Act! counterpart of any kind. Zero joinability. |
| `advisor_prior_registrations.*` | B | 55.6% of CRDs | No Act! counterpart. Zero joinability. |
| `advisor_employment_history.*` | B | 100% of 435,935 CRDs | No Act! counterpart. `custom.comments` (27.9%) occasionally names a prior firm in prose (`previously with SmithBarney`) — unstructured, not a join. |
| `advisor_employments.emp_street1` / `emp_postal` | B | 97.3% populated, **2.4% / 4.5% agreement** | Wide coverage, no signal. Firm HQ, not the person. Do not mistake its coverage for usefulness. |
| `firms.phone` | B | 99.98% populated, **1.9% agreement** | Same trap. |
| `businessAddress.country` | A | 10.2%, values like `B` | Corrupt. |
| `businessAddress.state` | A | 99.5% but **65 distinct values for ~50 states** | Needs normalisation before use. |

### One structural gap worth naming

`advisor_branches` gives 686,187 filed office addresses over 435,171 CRDs, and
54.6% of its `street + ZIP5` keys belong to exactly one advisor. Act! has a
street on 98.5% of contacts. That pairing is populated on both sides for **39,249
of 41,594 matched rows (94.4%)** — the highest both-populated rate of any
person-level signal in the inventory, and roughly ten times the reach of
independent email. It is also entirely unused by the current matcher, which reads
only `city` and `state` from the same address. Whatever the rule ends up being,
the street line is where the unexploited coverage is.
