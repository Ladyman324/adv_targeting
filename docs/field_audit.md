# Field audit — every mapped field checked against the Form ADV text

**Authority (verified current):** `docs/reference/formadv_part1a_current.txt` and
`formadv_instructions_current.txt` — **SEC 1707 (07-24)**, the July 2024 revision,
downloaded from sec.gov and confirmed current via the SEC Forms Index.

**Version chain, established rather than assumed:**

| Question | Answer | Evidence |
|---|---|---|
| Which electronic form version are firms filing under? | **10/2021** | 16,930 of 16,935 roster filings, including everything through 2026-06-30 |
| What is the current printed revision? | **SEC 1707 (07-24)** | Revision stamp in the PDF; SEC Forms Index entry |
| Does 07-24 differ from the 10/2021 filings for anything we use? | **No** | 18 item-text phrases spanning 5.B, 5.D, 5.G, 5.I, 5.K and 8 match exactly across both |

The `formadv.pdf` linked from the Forms Index as "Aug. 2022" is a one-page cover
sheet, not the form. The form ships as five separate PDFs.

**Discarded:** an earlier download of the instructions turned out to be SEC 1707
(10-10) — the October 2010 revision, predating Items 5.I(2), 5.J, 5.K and 5.L
entirely. It has been deleted rather than kept, because a stale reference document
is worse than none. It also carried a **materially different Glossary definition**:

| | High Net Worth Individual |
|---|---|
| 2010 | at least $750,000 managed by you, or net worth over $1,500,000, or a qualified purchaser |
| **Current (07-24)** | **a qualified client, or a qualified purchaser** (§2(a)(51)(A) Investment Company Act 1940) |

"Qualified client" is the Rule 205-3 inflation-adjusted standard — a higher bar than
the old fixed figures. `n_hnw_individuals` (Item 5.D(b)) therefore counts a wealthier
cohort than the 2010 text would suggest.

---

## Errors found and corrected

| # | Field | Was mapped to | Actually is | Impact |
|---|---|---|---|---|
| 1 | `emp_ins_agents` | 5B(3) | 5B(3) is **state-registered IARs**; insurance agents are **5B(5)** | Mislabelled output; not used in logic |
| 2 | `bonds_us_govt` / `bonds_muni` | iii / iv swapped | (iii) U.S. Government/Agency, (iv) State and Local | Mislabelled; not used in logic |
| 3 | `recommends_advisers` | 8F | 8F asks whether **recommended brokers are related persons** | **Used in 2 classification rules — 202 firms reclassified** |
| 4 | `compensated_for_recs` | 8G(1) | 8G(1) is **soft dollar benefits** | Mislabelled; not used in logic |
| 5 | `website` | roster's single Item 1.I value | Item 1.I asks for **social media accounts too** | 32.7% were LinkedIn/X profiles → now 1.5% |

**Item 8 contains no adviser-selection question at all.** Item 5.G(7) is the only one
in Part 1A. Classification now relies on it alone.

Error 3 was the consequential one. The mislabelled flag was shielding genuine asset
managers from the `asset_manager` rule — BlackRock Fund Advisors, Invesco Capital
Management, Apollo, Ares, and KKR were all classified as `institutional_only` or
`private_fund_shop`. 202 firms moved; only 13 left `distributor`.

## Documentation errors corrected

| Claim previously made | Form text |
|---|---|
| "Item 5.K reports look-through holdings, so an adviser who buys SMAs is indistinguishable from a manager who runs them" | *"Investments in derivatives, registered investment companies, business development companies, and pooled investment vehicles should be reported in those categories. **Do not report those investments based on related or underlying portfolio assets.**"* — no look-through for funds. Category (ix) does separate a fund allocator from a direct-securities book. The claim holds only for third-party SMAs, where the client legally owns the underlying securities. |
| "Item 5.K(2) is custodians" | 5.K(2) is **borrowing transactions**, 5.K(3) is **derivative transactions** (both feed Schedule D Section 5.K.(2)). Custodians are **5.K(4)** → Schedule D Section 5.K.(3). |
| "5.J(1)/(2) — the SMA gate" | 5.J(1) asks whether Part 2A Item 4.B indicates advice on **limited types of investments**; 5.J(2) whether Part 2A Item 4.E assets use a **different computation method**. Nothing to do with SMAs. Explains why 5J(1)=4,049 never reconciled with 5K(1)=12,401. |

---

## Confirmed correct

**Item 5.A / 5.B** — 5.A all employees excluding clerical; 5.B(1) perform investment
advisory functions including research; 5.B(2) registered representatives of a
broker-dealer. Verified against CRD 105010 (5A=17, 5B1=17, 5B2=17, 5B3=0, 5B5=9).

**Item 5.D — all fourteen client types** (a) individuals other than HNW, (b) HNW
individuals, (c) banking or thrift, (d) investment companies, (e) BDCs, (f) pooled
vehicles, (g) pension and profit sharing, (h) charitable, (i) state or municipal
government, (j) other investment advisers, (k) insurance companies, (l) sovereign
wealth, (m) corporations or other businesses, (n) other.

> The form specifies its own cross-check: *"The aggregate amount of regulatory assets
> under management reported in Item 5.D.(3) should equal the total amount reported in
> Item 5.F.(2)(c)."*
> **Result: passes for 16,933 of 16,935 firms, median difference $0.** Independent
> confirmation of both the client-type letters and the RAUM mapping.

**Item 5.F(2)** — (a) discretionary $, (b) non-discretionary $, (c) total $,
(d) discretionary accounts, (e) non-discretionary accounts, (f) total accounts.

**Item 5.G — all twelve**, including (3) portfolio management for investment
companies and (7) *"Selection of other advisers (including private fund managers)"*.

**Item 5.I** — (1) participate in a wrap fee program; (2)(a) sponsor $, (2)(b)
portfolio manager $, (2)(c) sponsor **and** portfolio manager for the same program $.

**Item 5.K(1)** — the gate for the allocation blocks; count matches the union of the
(a) and (b) blocks exactly (12,401).

**Schedule D 7.B.(1) fund types** — hedge, liquidity, private equity, real estate,
securitized asset, venture capital, other. Matches the roster's derived counts.

---

## Interpretation notes that change how fields should be read

- **5.K(1) percentages are of the non-pooled book**, not total RAUM: *"After
  subtracting the amounts reported in Item 5.D.(3)(d)-(f) from your total regulatory
  assets under management."* `pct_equity >= 80` means 80% of the non-pooled portion.
- **5.I(1) understates wrap involvement**: *"If your involvement in a wrap fee program
  is limited to **recommending** wrap fee programs to your clients... do not check
  Item 5.I.(1)."* Absence of wrap data is not absence of wrap activity — and
  recommending wrap programs is distributor behaviour.
- **"Individuals" in 5.D includes** trusts, estates, and 401(k)s and IRAs of
  individuals and their family members; excludes sole proprietorships.
- **5.C(1) is not a client count** — it counts clients *for whom you do not have*
  regulatory assets under management.
- **7.B(1) undercounts sub-advisers** — an adviser to a private fund reported by
  another adviser files Section 7.B.(2) instead. `n_private_funds` uses 7.B.(1).
- **`website_as_filed` is retained** alongside the improved `website`. 235 firms
  (1.5%) listed only social accounts, so their primary is still a social profile.
  A handful of others resolve oddly where a firm lists several unrelated domains.

## Rule for adding fields

Do not add a field to `COLS`, `ALLOC`, `CLIENT_TYPES`, or any classification rule
without quoting the form text for it in a comment. Four of the five errors above came
from inferring a meaning from a column label instead of reading the question.
