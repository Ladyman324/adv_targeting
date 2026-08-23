"""Load the SEC ADV firm-roster CSV and derive targeting features.

Column meanings below were verified empirically against the 2026-07-01 file
(see docs/data_dictionary.md) — not assumed from the Form ADV instructions.
"""
import sys, zipfile
import numpy as np
import pandas as pd

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1]))
import config

# --- Verified column map: semantic name -> raw CSV header ----------------
COLS = {
    "crd":              "Organization CRD#",
    "name":             "Primary Business Name",
    "legal_name":       "Legal Name",
    "city":             "Main Office City",
    "state":            "Main Office State",
    "postal":           "Main Office Postal Code",
    "phone":            "Main Office Telephone Number",
    "website":          "Website Address",
    "status":           "SEC Current Status",
    "filed":            "Latest ADV Filing Date",
    "n_other_offices":  "Total number of offices, other than your Principal Office and place of business",

    # Item 5.A/5.B verbatim from the filed form. NOTE: 5B(3) is state-registered
    # IARs, NOT insurance agents -- insurance agents are 5B(5). These were
    # transposed here originally.
    "emp_total":        "5A",      # all employees, excl. clerical
    "emp_advisory":     "5B(1)",   # perform investment advisory functions (incl. research)
    "emp_bd_reps":      "5B(2)",   # registered representatives of a broker-dealer
    "emp_state_iars":   "5B(3)",   # registered with state authorities as IARs
    "emp_iars_other_ia":"5B(4)",   # IARs for an investment adviser OTHER than you
    "emp_ins_agents":   "5B(5)",   # licensed agents of an insurance company or agency
    "n_solicitors":     "5B(6)",   # firms/persons who solicit advisory clients for you

    # Item 5.F(2): (c) = total RAUM $, (f) = total # accounts  [verified: a+b=c, d+e=f]
    "raum_disc":        "5F(2)(a)",
    "raum_nondisc":     "5F(2)(b)",
    "raum_total":       "5F(2)(c)",
    "acct_disc":        "5F(2)(d)",
    "acct_nondisc":     "5F(2)(e)",
    "acct_total":       "5F(2)(f)",

    # Item 5.G advisory activities  [5G(3) verified against RIC/BDC count column]
    "g_fin_planning":   "5G(1)",   # financial planning
    "g_pm_individuals": "5G(2)",   # portfolio mgmt for individuals / small businesses
    "g_pm_ric":         "5G(3)",   # portfolio mgmt for investment companies  <- manager tell
    "g_pm_pooled":      "5G(4)",   # portfolio mgmt for pooled vehicles
    "g_pm_institution": "5G(5)",   # portfolio mgmt for businesses / institutional clients
    "g_pension_consult":"5G(6)",
    "g_select_advisers":"5G(7)",   # SELECTION OF OTHER ADVISERS  <- the spine of the model
    "g_seminars":       "5G(11)",

    # Item 5.I wrap fee — (2)(a)/(b)/(c) are DOLLAR AMOUNTS, not flags
    "wrap_any":         "5I(1)",
    "wrap_sponsor_amt": "5I(2)(a)",   # sponsor  -> runs a platform (gatekeeper)
    "wrap_pm_amt":      "5I(2)(b)",   # portfolio manager -> a manager like us (competitor)
    "wrap_both_amt":    "5I(2)(c)",
    "wrap_n_programs":  "5.I.(2) - Total number of wrap fee programs",

    # 5.K(1): "Do you have regulatory assets under management attributable to
    # clients other than those listed in Item 5.D.(3)(d)-(f)?" The form calls these
    # "separately managed account clients", but that is a REGULATORY definition
    # meaning any non-pooled client -- it has nothing to do with industry SMA or
    # wrap platforms. Named accordingly so nobody reads it as "does SMA business".
    "has_non_pooled_raum": "5K(1)",

    # Item 7.B private funds
    "has_private_funds":"7B",
    "n_private_funds":  "Count of Private Funds - 7B(1)",
    "pf_gross_assets":  "Total Gross Assets of Private Funds",
    "n_hedge_funds":    "Total number of Hedge funds",
    "n_pe_funds":       "Total number of PE funds",
    "n_vc_funds":       "Total number of VC funds",

    # Item 8 -- verbatim from the filed form. These were badly mislabelled here:
    # 8.F is NOT "recommends other advisers" (it asks whether recommended
    # brokers are related persons) and 8.G(1) is NOT "compensated for
    # recommendations" (it is soft dollars). Item 8 contains no adviser-selection
    # question at all -- Item 5.G(7) is the only one in Part 1A.
    "recommends_brokers":       "8E",     # recommends brokers or dealers to clients
    "recommended_brokers_related": "8F",  # ...and those brokers are related persons
    "soft_dollars":             "8G(1)",  # receives research/products from a BD
    "pays_for_referrals":       "8H(1)",  # compensates non-employees for client referrals
    "emp_comp_for_new_clients": "8H(2)",  # employee comp tied to obtaining clients
    "receives_referral_comp":   "8I",     # receives compensation for client referrals
}

# Item 5.D client types: letter -> label. (n)(1)=# clients, (n)(3)=RAUM $.
CLIENT_TYPES = {
    "a": "individuals_non_hnw", "b": "hnw_individuals", "c": "banks",
    "d": "investment_companies", "e": "bdcs", "f": "pooled_vehicles",
    "g": "pensions", "h": "charities", "i": "govt_entities",
    "j": "other_advisers", "k": "insurance_cos", "l": "sovereign_wealth",
    "m": "corporations", "n": "other_clients",
}

# Item 5.K(1) asset categories, verbatim from the filed Form ADV. Block (a) =
# firms with >=$10B in the relevant base, (b) = <$10B.
#
# IMPORTANT -- the base is NOT total RAUM. Per the form: "After subtracting the
# amounts reported in Item 5.D.(3)(d)-(f) from your total regulatory assets under
# management" -- i.e. these percentages describe the NON-POOLED book only, with
# investment companies, BDCs, and pooled vehicles removed from the denominator.
#
# Also per the form: "Investments in derivatives, registered investment companies,
# business development companies, and pooled investment vehicles should be reported
# in those categories. Do NOT report those investments based on related or
# underlying portfolio assets." So there is no look-through for fund holdings --
# category (ix) genuinely separates a fund allocator from a direct securities book.
ALLOC = {
    "i": "equity_exchange_traded", "ii": "equity_non_exchange",
    "iii": "bonds_us_govt", "iv": "bonds_muni", "v": "bonds_sovereign",
    "vi": "bonds_corp_ig", "vii": "bonds_corp_hy", "viii": "derivatives",
    "ix": "fund_shares_ric", "x": "pooled_vehicle_shares",
    "xi": "cash", "xii": "alloc_other",
}


def _num(s: pd.Series) -> pd.Series:
    """ADV numerics arrive space-padded with commas and bare '.00' for zero."""
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False).str.strip(),
                         errors="coerce")


def _flag(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.upper().eq("Y")


def load_raw() -> pd.DataFrame:
    """Read the firm roster from either the SEC download ZIP or a loose CSV."""
    src = config.ADV_SOURCE
    if src.suffix.lower() == ".zip":
        z = zipfile.ZipFile(src)
        handle = z.open(next(n for n in z.namelist() if n.lower().endswith(".csv")))
    else:
        handle = src
    return pd.read_csv(handle, encoding="latin-1", low_memory=False, dtype=str)


def build_features(raw: pd.DataFrame, keep_all: bool = True) -> pd.DataFrame:
    """Derive the curated feature set.

    `keep_all` carries EVERY remaining roster column through under its original
    SEC header, so nothing is silently lost. An earlier version mapped only ~100
    of the 448 columns and dropped the rest without saying so -- including CIK#
    (the EDGAR/13F join key), all of Item 11 (disciplinary), Item 7.A
    (affiliations), Item 9 (custody), and every mid-year allocation figure.
    Curated snake_case names and raw SEC headers never collide.
    """
    d = pd.DataFrame(index=raw.index)

    for sem, col in COLS.items():
        d[sem] = raw[col]

    if keep_all:
        used = set(COLS.values())
        for letter in CLIENT_TYPES:
            used |= {f"5D({letter})(1)", f"5D({letter})(3)"}
        for roman in ALLOC:
            used |= {f"5.K.(1)(a)({roman}) end year percentage",
                     f"5.K.(1)(b)({roman}) end year percentage"}
        for col in raw.columns:
            if col not in used:
                d[col] = raw[col]

    # numeric / boolean coercion
    for c in ["emp_total", "emp_advisory", "emp_bd_reps", "emp_state_iars",
              "emp_iars_other_ia", "emp_ins_agents", "n_solicitors",
              "raum_disc", "raum_nondisc", "raum_total", "acct_disc",
              "acct_nondisc", "acct_total", "wrap_sponsor_amt", "wrap_pm_amt",
              "wrap_both_amt", "wrap_n_programs", "n_private_funds",
              "pf_gross_assets", "n_hedge_funds", "n_pe_funds", "n_vc_funds",
              "n_other_offices"]:
        d[c] = _num(d[c])
    for c in [k for k in COLS if k.startswith("g_")] + [
              "wrap_any", "has_non_pooled_raum", "has_private_funds",
              "recommends_brokers", "recommended_brokers_related", "soft_dollars",
              "pays_for_referrals", "emp_comp_for_new_clients",
              "receives_referral_comp"]:
        d[c] = _flag(d[c])

    # --- Item 5.D client mix ---
    for letter, label in CLIENT_TYPES.items():
        d[f"n_{label}"]    = _num(raw.get(f"5D({letter})(1)", pd.Series(index=raw.index)))
        d[f"raum_{label}"] = _num(raw.get(f"5D({letter})(3)", pd.Series(index=raw.index)))

    d["n_retail_clients"]    = d["n_individuals_non_hnw"].fillna(0) + d["n_hnw_individuals"].fillna(0)
    d["raum_retail"]         = d["raum_individuals_non_hnw"].fillna(0) + d["raum_hnw_individuals"].fillna(0)
    d["raum_pooled_and_ric"] = d["raum_pooled_vehicles"].fillna(0) + d["raum_investment_companies"].fillna(0)

    # --- Item 5.K(1) allocation: coalesce the >=$10B block onto the <$10B block ---
    for roman, label in ALLOC.items():
        a = _num(raw.get(f"5.K.(1)(a)({roman}) end year percentage", pd.Series(index=raw.index)))
        b = _num(raw.get(f"5.K.(1)(b)({roman}) end year percentage", pd.Series(index=raw.index)))
        d[f"pct_{label}"] = a.combine_first(b)

    d["pct_equity"] = d["pct_equity_exchange_traded"].fillna(0) + d["pct_equity_non_exchange"].fillna(0)
    d["has_alloc_data"] = d["pct_equity_exchange_traded"].notna()

    # --- derived ratios ---
    d["avg_account_size"] = (d["raum_total"] / d["acct_total"]).replace([np.inf, -np.inf], np.nan)
    d["raum_per_client"]  = (d["raum_total"] / d["n_retail_clients"].replace(0, np.nan))
    d["retail_raum_share"] = (d["raum_retail"] / d["raum_total"].replace(0, np.nan)).clip(0, 1)
    d["pooled_raum_share"] = (d["raum_pooled_and_ric"] / d["raum_total"].replace(0, np.nan)).clip(0, 1)
    # Section 5.K(1) percentages apply only after removing client RAUM reported
    # in 5.D(3)(d)-(f). The implied dollars below are estimates because the filed
    # percentages are rounded to the nearest whole percent.
    excluded = d[["raum_investment_companies", "raum_bdcs", "raum_pooled_vehicles"]].fillna(0).sum(axis=1)
    d["raum_non_pooled"] = (d["raum_total"].fillna(0) - excluded).clip(lower=0)
    d["raum_equity_exchange_implied"] = (
        d["raum_non_pooled"] * d["pct_equity_exchange_traded"] / 100)
    d["raum_fund_shares_ric_implied"] = (
        d["raum_non_pooled"] * d["pct_fund_shares_ric"] / 100)
    d["discretionary_share"] = (
        d["raum_disc"] / d["raum_total"].replace(0, np.nan)).clip(0, 1)

    # a firm that manages wrap money but sponsors none is a manager, not a distributor
    d["is_wrap_sponsor"] = d["wrap_sponsor_amt"].fillna(0) + d["wrap_both_amt"].fillna(0) > 0
    d["is_wrap_pm_only"] = (d["wrap_pm_amt"].fillna(0) > 0) & ~d["is_wrap_sponsor"]
    d["is_bd_hybrid"]    = d["emp_bd_reps"].fillna(0) > 0

    return d


def attach_advisors(d: pd.DataFrame) -> pd.DataFrame:
    """Merge per-firm advisor counts from the IA_INDVL compilation feed.

    Firms absent from the feed have no registered representatives -- Vanguard,
    Fidelity, PIMCO and the like -- which is itself a signal: no reps means no
    distribution channel, whatever the AUM.
    """
    roll_path = config.INTERIM / "firm_advisor_rollup.parquet"
    if not roll_path.exists():
        # EVERY derived column this function owns must exist on both paths.
        # Only n_advisors was created here, so score.py -- which reads
        # aum_per_advisor unconditionally -- died with a KeyError on any clean
        # rebuild. NaN, not 0: "we have no advisor feed" is not "this firm has
        # no advisors", and the scorer's log scale treats them very differently.
        d["n_advisors"] = np.nan
        d["aum_per_advisor"] = np.nan
        return d
    roll = pd.read_parquet(roll_path)
    roll["firm_crd"] = roll["firm_crd"].astype(str)
    out = d.merge(roll, how="left", left_on="crd", right_on="firm_crd")
    out = out.drop(columns=["n_states"], errors="ignore")  # was firm-HQ derived; always 1
    for c in ["n_advisors", "n_branches", "n_cfp"]:
        if c in out:
            out[c] = out[c].fillna(0)
    out["aum_per_advisor"] = (out["raum_total"]
                              / out["n_advisors"].replace(0, np.nan))
    return out.drop(columns=["firm_crd"], errors="ignore")


def attach_websites(d: pd.DataFrame) -> pd.DataFrame:
    """Replace the roster's single Item 1.I address with a preferred one.

    Item 1.I asks for social media accounts alongside websites, and the roster
    CSV keeps only one -- a LinkedIn or X profile for 32.7% of firms. The XML
    feed has the full list, so we can prefer a real domain where one exists.
    """
    p = config.INTERIM / "firm_website_primary.parquet"
    if not p.exists():
        return d
    w = pd.read_parquet(p)
    w["firm_crd"] = w["firm_crd"].astype(str)
    out = d.merge(w, how="left", left_on="crd", right_on="firm_crd")
    out = out.rename(columns={"website": "website_as_filed"})
    out["website"] = out["website_primary"].fillna(out["website_as_filed"])
    return out.drop(columns=["firm_crd", "website_primary"], errors="ignore")


def load() -> pd.DataFrame:
    return attach_websites(attach_advisors(build_features(load_raw())))
