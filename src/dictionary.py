"""Build a field dictionary table for the Power BI model.

Generated from the live mappings in adv.py/score.py, so it cannot drift out of
sync with the data. Curated notes below carry the things the code cannot know:
verbatim form wording, and the caveats that make a field easy to misread.

Load `field_dictionary.parquet` into Power BI as a disconnected reference table.
"""
from __future__ import annotations

import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))
import adv, score as scoring, config  # noqa: E402

FORM = "Form ADV Part 1A, SEC 1707 (07-24)"

# column -> (friendly label, definition, caveat)
NOTES = {
    "crd": ("Firm CRD", "Organization CRD number. The stable key for a firm; names are not unique.", ""),
    "firm_display": ("Firm", "Name, qualified with state and CRD where the name is shared by more than one firm.", "Use this for grouping, not `name` -- 57 names are shared by 123 firms (two 'CAPITAL MANAGEMENT ASSOCIATES INC', five 'RUSSELL INVESTMENTS')."),
    "name": ("Firm Name (as filed)", "Primary business name from Item 1.", "Not unique. Grouping by this silently merges distinct firms."),
    "website": ("Website", "Preferred Item 1.I address -- a real domain where the firm listed one.", "Item 1.I asks for social media accounts too. 235 firms (1.5%) listed only social profiles."),
    "website_as_filed": ("Website (roster value)", "The single Item 1.I address carried in the SEC roster CSV.", "32.7% of these are LinkedIn/X profiles rather than corporate sites."),

    "emp_total": ("Employees", "Item 5.A: all employees, full and part time, excluding clerical.", ""),
    "emp_advisory": ("Advisory Employees", "Item 5.B(1): employees performing investment advisory functions, including research.", "Correlates 0.94 with registered-rep headcount but measures a different population."),
    "emp_bd_reps": ("Employees who are BD Reps", "Item 5.B(2): employees who are registered representatives of a broker-dealer.", ""),
    "emp_state_iars": ("Employees registered as IARs", "Item 5.B(3): employees registered with state authorities as investment adviser representatives.", "Was previously mislabelled as insurance agents. Insurance agents are 5.B(5)."),
    "emp_ins_agents": ("Employees who are Insurance Agents", "Item 5.B(5): licensed agents of an insurance company or agency.", ""),
    "n_solicitors": ("Solicitor Firms", "Item 5.B(6): firms or persons who solicit advisory clients on the firm's behalf.", ""),

    "raum_total": ("Regulatory AUM", "Item 5.F(2)(c): total regulatory assets under management.", "RAUM is a defined regulatory calculation, not the same as marketed AUM."),
    "raum_disc": ("RAUM - Discretionary", "Item 5.F(2)(a).", ""),
    "raum_nondisc": ("RAUM - Non-Discretionary", "Item 5.F(2)(b).", "A largely non-discretionary book is harder to place a strategy into -- every allocation needs client sign-off."),
    "acct_total": ("Accounts", "Item 5.F(2)(f): total number of accounts.", ""),
    "avg_account_size": ("Avg Account Size", "raum_total / acct_total.", "Derived, not reported."),

    "n_retail_clients": ("Retail Clients", "Item 5.D(a)(1) + 5.D(b)(1): individuals plus high-net-worth individuals.", "'Individuals' includes trusts, estates, and 401(k)s/IRAs of individuals and family members."),
    "n_hnw_individuals": ("HNW Clients", "Item 5.D(b)(1): high net worth individuals.", "Current Glossary defines HNW as a qualified client or qualified purchaser -- a higher bar than the pre-2016 $750K/$1.5M test."),
    "retail_raum_share": ("Retail Share of RAUM", "Individual + HNW RAUM as a share of total.", "Derived."),

    "g_select_advisers": ("Selects Other Advisers", "Item 5.G(7): 'Selection of other advisers (including private fund managers)'.", "A reported firm-level signal, not proof that the firm is receptive to EIC or to a specific product."),
    "g_pm_ric": ("Manages Registered Funds", "Item 5.G(3): portfolio management for investment companies / BDCs.", "Strong asset-manager signal. Verified: every firm checking it reports at least one RIC."),
    "g_fin_planning": ("Financial Planning", "Item 5.G(1).", ""),

    "wrap_any": ("Participates in Wrap", "Item 5.I(1).", "UNDERSTATES wrap activity: firms whose involvement is limited to RECOMMENDING wrap programs are told not to check this."),
    "wrap_sponsor_amt": ("Wrap RAUM - as Sponsor", "Item 5.I(2)(a): RAUM from acting as sponsor to a wrap fee program.", "Sponsors run the platform -- Merrill, Ameriprise, Edward Jones."),
    "wrap_pm_amt": ("Wrap RAUM - as Portfolio Manager", "Item 5.I(2)(b): RAUM from acting as portfolio manager in someone else's wrap program.", "This is what we are. High share here means competitor, not customer."),
    "wrap_pm_share": ("Wrap PM Share of RAUM", "wrap_pm_amt / raum_total.", "The proportional test that separates a manager (EIC, 78%) from an aggregator with an incidental sleeve (Mariner, 0.06%)."),

    "has_non_pooled_raum": ("Has Non-Pooled RAUM", "Item 5.K(1): RAUM attributable to clients other than funds and pooled vehicles.", "The form calls these 'separately managed account clients', but that is a REGULATORY definition meaning any non-pooled client. NOT industry SMA or wrap business."),
    "pct_equity": ("Equity % of Non-Pooled Book", "Item 5.K(1) categories (i)+(ii).", "Base EXCLUDES funds and pooled vehicles -- it is a share of the non-pooled book, not of total RAUM."),
    "pct_fund_shares_ric": ("Fund Shares % of Non-Pooled Book", "Item 5.K(1)(ix): securities issued by registered investment companies or BDCs.", "No look-through: fund holdings report as fund shares. This genuinely separates a fund allocator from a direct-securities book."),
    "has_alloc_data": ("Has Allocation Data", "Whether the firm completed a 5.K(1) block.", "12,401 of 16,935 firms."),
    "raum_non_pooled": ("Non-Pooled RAUM", "Total RAUM less Item 5.D assets for investment companies, BDCs, and pooled investment vehicles; the denominator for Item 5.K percentages.", "Derived. This is the filing's applicable non-pooled base, not industry SMA assets."),
    "raum_equity_exchange_implied": ("Implied Exchange-Traded Equity RAUM", "Non-pooled RAUM multiplied by Item 5.K(1)(i).", "Estimated product-relevant pool for EIC equity SMAs. Reported percentages are approximate; this is not a reported holding amount."),
    "raum_fund_shares_ric_implied": ("Implied RIC / BDC Securities RAUM", "Non-pooled RAUM multiplied by Item 5.K(1)(ix).", "Estimated product-relevant pool for the EIC mutual fund. The category includes mutual funds, ETFs, and BDC securities; no look-through is available."),
    "discretionary_share": ("Discretionary Share", "Discretionary RAUM divided by total RAUM.", "Do not apply this share to an asset category: ADV does not cross-tab discretion by asset type."),

    "n_advisors": ("Advisors", "Registered investment adviser representatives at the firm, from the IA_INDVL compilation feed.", "External source, not Form ADV. Differs from Item 5.B(1) -- EIC shows 16 reps against 27 advisory employees."),
    "n_cfp": ("CFP Advisors", "Advisors carrying the Certified Financial Planner designation.", "From the IA_INDVL feed."),
    "n_branch_states": ("States Covered", "Distinct states in which the firm's advisers have branch offices.", "From branch locations, not firm HQ."),
    "aum_per_advisor": ("AUM per Advisor", "raum_total / n_advisors.", "Cross-firm differences reflect firm mix, NOT adviser productivity."),

    "opportunity_score": ("Opportunity Score", f"Two equal axes, 0..100. SIZE ({sum(w for w, _ in scoring.SIZE_COMPONENTS.values())} pts: advisor count, assets, assets per advisor, log-scaled) + FIT ({sum(w for w, _ in scoring.FIT_COMPONENTS.values())} pts: Item 5.G(7) selects-advisers {scoring.FIT_COMPONENTS['selects_advisers'][0]}, wrap sponsor {scoring.FIT_COMPONENTS['wrap_sponsor'][0]}).", "SIZE says how big the opportunity is if they buy; FIT says whether they buy at all. FIT calibrated on six hand-labelled firms where 5.G(7) separated fit from not-fit 3/3 -- a hypothesis to re-fit as more labels arrive."),
    "opportunity_score_size": ("Opportunity Score (size only)", "The old score before FIT existed: advisor count + assets + assets per advisor, log-scaled, rescaled to 0..100.", "Kept for audit. On the first six labelled firms this ran BACKWARDS -- the three non-fits outranked two of the three real prospects on size alone."),
    "opportunity_score_fit": ("Fit Score", f"FIT axis alone, 0..{sum(w for w, _ in scoring.FIT_COMPONENTS.values())}: Item 5.G(7) selects-advisers + wrap sponsor.", "Whether the firm hires outside managers, independent of size."),
    "firm_type": ("Firm Type", "How the firm relates to us: distributor, asset_manager, family_office_or_consultant, institutional_only, insufficient_data, private_fund_shop, no_registered_reps, sub_scale, not_registered.", "An attribute, not a filter. Nothing is removed from the table. insufficient_data means the firm reported no RAUM, which leaves the retail SHARE undefined -- it is a gap in the filing, not a finding about their business, and deliberately not the same bucket as sub_scale, which is a firm we measured and found small."),
    "sma_fit_score": ("Equity SMA Fit Score", "Transparent 0..100 prospecting heuristic using outside-manager selection, implied exchange-traded equity RAUM, account size, discretion, and retail mix.", "Not a purchase probability; calibrate against sales outcomes."),
    "fund_fit_score": ("Mutual Fund Fit Score", "Transparent 0..100 prospecting heuristic using implied RIC/BDC securities RAUM, allocation percentage, adviser count, retail mix, and outside-manager selection.", "Not a purchase probability; Item 5.K(1)(ix) includes ETFs and BDC securities as well as mutual funds."),
    "motion": ("Product Opportunity", "sma_led, eicix_led, both_products, or low_fit.", "A product recommendation, kept separate from sales access structure."),
    "platform_access": ("Platform / Home-Office Access", "True for wrap sponsors or firms above the configured gatekeeper RAUM threshold.", "An access constraint, not a product category."),
    "sales_channel": ("Sales Channel", "territory or home_office, derived from platform_access.", "Routing guidance only."),
    "is_target": ("Is Target", "firm_type = distributor.", ""),

    # --- DRP (Disclosure Reporting Page) flags on the advisors table ---------
    # The FINRA XSD and XML guide define these only circularly ("has Judgment
    # DRP"). Labels below are SEC's own `disclosureType` values, read from
    # api.adviserinfo.sec.gov for individuals flagged on ONE category only.
    "hasCriminal": ("Criminal Disclosure", "SEC disclosureType: 'Criminal'.", ""),
    "hasRegAction": ("Regulatory Disclosure", "SEC disclosureType: 'Regulatory'.", ""),
    "hasCivilJudc": ("Civil Disclosure", "SEC disclosureType: 'Civil'.", "Rare -- 0.03% of advisors."),
    "hasCustComp": ("Customer Dispute", "SEC disclosureType: 'Customer Dispute'.", "By far the most common at 9.2%. A dispute is an allegation, not a finding."),
    "hasTermination": ("Employment Separation After Allegations", "SEC disclosureType: 'Employment Separation After Allegations'.", "NOT a plain termination -- it means the person left a firm amid allegations. More serious than the attribute name suggests."),
    "hasJudgment": ("Judgment / Lien", "SEC disclosureType: 'Judgment / Lien' -- an unsatisfied judgment or lien against the individual. Detail carries amount and type (e.g. Tax).", "0.79% of advisors. Distinct from Civil (court proceedings) and Financial (bankruptcy)."),
    "hasBankrupt": ("Financial", "SEC disclosureType: 'Financial' -- bankruptcy, compromise with creditors and similar.", "SEC's category is 'Financial', broader than the attribute name 'hasBankrupt' implies."),
    "hasBond": ("Civil Bond", "SEC disclosureType: 'Civil Bond'.", "Rare -- 0.02%."),
    "hasInvstgn": ("Investigation", "SEC disclosureType: 'Investigation'.", "Rare -- 0.02%. An open investigation, not a finding."),
    "designations": ("Designations", "Professional designations, pipe-delimited (e.g. Certified Financial Planner).", ""),
    "n_exams": ("Exams Passed", "Count of qualifying examinations on record.", ""),
    "n_prior_firms": ("Prior Firms", "Count of previous registrations.", "High counts can indicate frequent moves."),

    "aum_allocated": ("Est AUM at this Location", "Firm RAUM split equally per advisor, then per branch.", "ESTIMATE. Form ADV reports assets at firm level only; assumes every adviser manages an equal share."),
    "iapd_url": ("IAPD Profile", "Direct link to the adviser's public IAPD record.", ""),
    "exam_code": ("Exam Code", "Qualifying examination code (e.g. Series 65).", ""),
    "from_date": ("Employment From", "Start of a prior employment, MM/YYYY.", "Employment history includes non-securities roles."),
    "reg_begin": ("Registration Begin", "Start of a prior registration with that firm.", ""),
    "reg_end": ("Registration End", "End of a prior registration -- the adviser left that firm.", "Basis for advisor-movement analysis."),
    "regulator_code": ("Jurisdiction", "State or territory regulator code for a notice filing.", ""),
    "description": ("Other Business", "Free-text description of outside business activity.", "Self-reported, uncontrolled text."),

    "review_reason": ("Review Flag", "Boundary cases Form ADV cannot resolve.", "A firm can genuinely be both a manager and a distributor; these are surfaced rather than silently classified."),
}

SOURCE_FEED = "IA_INDVL compilation feed (adviserinfo.sec.gov)"


def build() -> pd.DataFrame:
    rows = []
    seen = set()

    def add(col, table, source, form_ref):
        label, definition, caveat = NOTES.get(col, ("", "", ""))
        rows.append({"table": table, "column": col, "label": label or col,
                     "definition": definition, "caveat": caveat,
                     "source": source, "form_reference": form_ref,
                     "used_in_score": col in {"n_advisors", "raum_total", "aum_per_advisor"},
                     "used_in_classification": col in {
                         "is_wrap_pm_only", "wrap_pm_share", "g_pm_ric", "g_pm_pooled",
                         "g_select_advisers", "n_private_funds", "pooled_raum_share",
                         "retail_raum_share", "n_advisors", "n_retail_clients",
                         "raum_total", "status"}})
        seen.add(col)

    for col, raw in adv.COLS.items():
        ref = f"{FORM}, Item {raw}" if raw[0].isdigit() else f"SEC roster column: {raw}"
        add(col, "firms", "Form ADV Part 1A (SEC roster CSV)", ref)
    for letter, lbl in adv.CLIENT_TYPES.items():
        add(f"n_{lbl}", "firms", "Form ADV Part 1A", f"{FORM}, Item 5.D({letter})(1) - number of clients")
        add(f"raum_{lbl}", "firms", "Form ADV Part 1A", f"{FORM}, Item 5.D({letter})(3) - RAUM")
    for roman, lbl in adv.ALLOC.items():
        add(f"pct_{lbl}", "firms", "Form ADV Schedule D", f"{FORM}, Section 5.K(1)({roman}) - end of year %")

    firms = pd.read_parquet(config.OUTPUT / "firms.parquet")
    for col in firms.columns:
        if col not in seen:
            src = SOURCE_FEED if col.startswith(("n_advisors", "n_cfp", "n_branch", "aum_per")) else "Derived"
            add(col, "firms", src, "")
    # every non-firm table in data/output, so a new table can never be omitted
    for path in sorted(config.OUTPUT.glob("*.parquet")):
        tbl = path.stem
        if tbl in ("firms", "field_dictionary"):
            continue
        src = ("Form ADV firm XML feed" if tbl.startswith("firm_")
               else SOURCE_FEED)
        for col in pd.read_parquet(path).columns:
            seen.discard(col)
            add(col, tbl, src, "")

    return pd.DataFrame(rows).drop_duplicates(subset=["table", "column"])


if __name__ == "__main__":
    df = build()
    df.to_parquet(config.OUTPUT / "field_dictionary.parquet", index=False)
    df.to_csv(config.OUTPUT / "field_dictionary.csv", index=False)
    print(f"{len(df):,} field definitions")
    print(df.groupby("table").size().to_string())
    print(f"\nwith a written definition : {(df.definition != '').sum():,}")
    print(f"with a caveat             : {(df.caveat != '').sum():,}")
