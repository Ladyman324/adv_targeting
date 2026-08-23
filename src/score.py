"""Firm classification, fit scoring, and product-opportunity assignment.

Nothing is filtered out. Every firm keeps a firm_type, a score with its
component contributions, and any review flag, so the BI layer can decide.

Field meanings are verified against the filed Form ADV Part 1A (v10/2021) in
docs/reference/ -- see docs/field_audit.md. Do not add a field here without
quoting the form text for it.
"""
import sys, pathlib
import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
import config

def _pm_share(d: pd.DataFrame) -> pd.Series:
    """Wrap-portfolio-manager assets as a share of the firm's total RAUM."""
    return (d["wrap_pm_amt"].fillna(0) / d["raum_total"].replace(0, np.nan)).fillna(0)


# Firms that survive exclusion but sit on the manager/distributor boundary. Form ADV
# cannot resolve this -- a firm can be both -- so surface them with their evidence
# instead of making a silent binary call that has now been wrong in both directions.
REVIEW = [
    ("wrap_pm_but_distributor_shaped",
     lambda d: (d["is_wrap_pm_only"] & (_pm_share(d) < config.WRAP_PM_MATERIAL)
                & d["g_select_advisers"]
                & (d["n_retail_clients"].fillna(0) >= 200))),

    ("manager_risk_but_scores_well",
     lambda d: (~d["g_fin_planning"] & ~d["is_bd_hybrid"]
                & (d["pct_equity"].fillna(0) >= 80)
                & (d["pct_fund_shares_ric"].fillna(0) < 10)
                & d["has_alloc_data"])),
]

# The score has two axes that answer two different questions, weighted equally.
# Keeping them separate was the point: the old score was SIZE only, so it ranked
# how big the opportunity would be if the firm bought, with nothing about whether
# they buy at all. Measured on the first six hand-labelled firms (3 fit, 3 not),
# the size-only score ran BACKWARDS -- the three non-fits averaged 53.4 and
# outranked two of the three real prospects, purely because they were larger.
#
#   SIZE  -- how big is the opportunity if they buy       (0..50)
#   FIT   -- will they buy at all                          (0..50)
#
# FIT is anchored on Item 5.G(7). On those six it separated fit from not-fit
# perfectly (3/3 vs 3/3); its base rate among targets is 50%, so it discriminates
# rather than gates. Three signals proposed alongside it were dropped after the
# same six showed they do not belong:
#   * wrap_any        -- contaminated: TRUE for all three non-fits, because
#                        running wrap AS the portfolio manager is the asset-
#                        manager tell, not a distribution signal. Only the clean
#                        is_wrap_sponsor (runs an SMA platform) is kept.
#   * pct_equity      -- does not separate; EIC itself is 93% equity. It says
#                        WHICH product, not WHETHER they hire managers.
#   * g_fin_planning  -- 87% near-universal among targets; no ranking power.
# This is calibrated on six points, so it is a hypothesis to be re-fit when more
# labels arrive -- see docs/field_audit.md. Weights are meant to be tuned.

# SIZE: unchanged inputs, rescaled from 100 to 50 so FIT can occupy the other half.
SIZE_COMPONENTS = {
    "advisor_count":      (20, lambda d: _log_scale(d["n_advisors"], 1, 5_000)),
    "assets":             (20, lambda d: _log_scale(d["raum_total"], 50e6, 100e9)),
    "assets_per_advisor": (10, lambda d: _log_scale(d["aum_per_advisor"], 10e6, 1e9)),
}

# FIT: 5.G(7) manager-selection is the anchor; a wrap SPONSOR runs SMA platform
# infrastructure and gets a smaller bump on top.
FIT_COMPONENTS = {
    "selects_advisers": (40, lambda d: d["g_select_advisers"].fillna(False).astype(float)),
    "wrap_sponsor":     (10, lambda d: d["is_wrap_sponsor"].fillna(False).astype(float)),
}

# The score reported to the sales team: both axes combined, still 0..100 so it
# stays comparable to what came before.
COMPONENTS = {**SIZE_COMPONENTS, **FIT_COMPONENTS}

# How a firm relates to us -- an ATTRIBUTE, not a filter. Nothing is dropped;
# the sales team filters in the BI layer. Ordered, first match wins.
FIRM_TYPES = [
    ("not_registered",
     lambda d: ~d["status"].astype(str).str.upper().str.contains("APPROVED|REGISTERED", na=False)),
    ("asset_manager",
     lambda d: ((d["is_wrap_pm_only"] & (_pm_share(d) >= config.WRAP_PM_MATERIAL))
                | (d["g_pm_ric"] & d["is_wrap_pm_only"])
                | ((d["g_pm_ric"] | d["g_pm_pooled"])
                   & ~d["g_select_advisers"]))),
    ("private_fund_shop",
     lambda d: (d["n_private_funds"].fillna(0) > 0) & (d["pooled_raum_share"].fillna(0) > 0.5)),
    # ABSENT DATA IS NOT EVIDENCE. This sat one line lower and read
    # retail_raum_share.fillna(0), so a firm that reported no RAUM at all --
    # which makes the retail SHARE undefined, not zero -- was labelled
    # "institutional_only": an affirmative claim about a business model, made
    # from a blank. It has to be caught BEFORE the rules that would read the
    # blank as a number, and it is deliberately not the same bucket as
    # sub_scale, which is a firm we measured and found small.
    ("insufficient_data",
     lambda d: ~(d["raum_total"] > 0) | d["retail_raum_share"].isna()),
    ("institutional_only",
     lambda d: d["retail_raum_share"] < config.MIN_RETAIL_RAUM_SHARE),
    ("no_registered_reps",
     lambda d: d["n_advisors"].fillna(0) == 0),
    ("family_office_or_consultant",
     lambda d: d["n_retail_clients"].fillna(0) < config.MIN_RETAIL_CLIENTS),
    ("sub_scale",
     lambda d: d["raum_total"].fillna(0) < config.MIN_RAUM),
]


def _log_scale(s: pd.Series, lo: float, hi: float) -> pd.Series:
    """Map [lo, hi] onto 0..1 on a log scale; below lo -> 0, above hi -> 1."""
    v = pd.to_numeric(s, errors="coerce").fillna(0).clip(lower=0)
    out = (np.log10(v.clip(lower=lo)) - np.log10(lo)) / (np.log10(hi) - np.log10(lo))
    return out.clip(0, 1).where(v > 0, 0.0)


def classify(d: pd.DataFrame) -> pd.Series:
    """Assign firm_type. Every firm gets one; nothing is removed."""
    out = pd.Series(pd.NA, index=d.index, dtype="object")
    for name, rule in FIRM_TYPES:
        out[rule(d).fillna(False) & out.isna()] = name
    return out.fillna("distributor")


def apply_review(d: pd.DataFrame, firm_type: pd.Series) -> pd.Series:
    """Boundary cases Form ADV cannot resolve -- surfaced, not silently binned."""
    reason = pd.Series(pd.NA, index=d.index, dtype="object")
    for name, rule in REVIEW:
        reason[rule(d).fillna(False) & reason.isna()] = name
    return reason


def score(d: pd.DataFrame, components=None) -> pd.DataFrame:
    out = pd.DataFrame(index=d.index)
    total = pd.Series(0.0, index=d.index)
    for name, (weight, fn) in (components or COMPONENTS).items():
        pts = fn(d).astype(float).fillna(0) * weight
        out[f"pts_{name}"] = pts.round(1)
        total += pts
    out["opportunity_score"] = total.round(1)
    return out


def product_scores(d: pd.DataFrame) -> pd.DataFrame:
    """Sales-facing product relevance, kept separate from access structure.

    These are transparent starting heuristics, not trained probabilities. The
    dollar components use the non-pooled 5.K denominator and therefore describe
    potentially relevant pools, not assets demonstrably available to EIC.
    """
    out = pd.DataFrame(index=d.index)
    sma = (
        30 * d["g_select_advisers"].fillna(False).astype(float)
        + 35 * _log_scale(d["raum_equity_exchange_implied"], 25e6, 5e9)
        + 15 * _log_scale(d["avg_account_size"], 250e3, 3e6)
        + 10 * d["discretionary_share"].fillna(0)
        + 10 * d["retail_raum_share"].fillna(0)
    )
    fund = (
        40 * _log_scale(d["raum_fund_shares_ric_implied"], 10e6, 2e9)
        + 20 * (d["pct_fund_shares_ric"].fillna(0) / 60).clip(0, 1)
        + 20 * _log_scale(d["n_advisors"], 1, 1_000)
        + 15 * d["retail_raum_share"].fillna(0)
        + 5 * d["g_select_advisers"].fillna(False).astype(float)
    )
    out["sma_fit_score"] = sma.round(1)
    out["fund_fit_score"] = fund.round(1)
    return out


def assign_motion(d: pd.DataFrame, firm_type: pd.Series, product: pd.DataFrame) -> pd.Series:
    """Primary product opportunity for territory sales; products may both fit."""
    threshold = config.PRODUCT_FIT_MIN
    sma = product["sma_fit_score"] >= threshold
    fund = product["fund_fit_score"] >= threshold
    target = firm_type == "distributor"
    m = pd.Series("low_fit", index=d.index, dtype="object")
    m[target & sma & fund] = "both_products"
    m[target & sma & ~fund] = "sma_led"
    m[target & fund & ~sma] = "eicix_led"
    return m


def run(d: pd.DataFrame) -> pd.DataFrame:
    firm_type = classify(d)
    scores    = score(d)
    products  = product_scores(d)
    # The old size-only ranking, kept alongside so the shift from size to
    # size+fit is auditable: opportunity_score_size is what the number was
    # before FIT existed. Scaled back to 0..100 (SIZE alone tops out at 50).
    size_only = (score(d, SIZE_COMPONENTS)["opportunity_score"] * 2).round(1) \
                    .rename("opportunity_score_size")
    fit_only  = score(d, FIT_COMPONENTS)["opportunity_score"].rename("opportunity_score_fit")
    result    = pd.concat([d, scores, size_only, fit_only, products], axis=1)
    result["firm_type"]     = firm_type
    result["review_reason"] = apply_review(d, firm_type)
    result["wrap_pm_share"] = _pm_share(d).round(4)
    result["platform_access"] = d["is_wrap_sponsor"] | (d["raum_total"].fillna(0) > config.GATEKEEPER_RAUM)
    result["sales_channel"] = np.where(result["platform_access"], "home_office", "territory")
    result["motion"]        = assign_motion(d, firm_type, products)
    result["is_target"]     = firm_type == "distributor"

    # A field-sales firm at 58 and a wirehouse at 92 are not competing for the
    # same slot -- the raw score ranks scale, so platforms crowd out every other
    # product group. Percentile WITHIN product opportunity lets each team sort its own list top-down.
    tgt = result["is_target"]
    result["score_pct_in_motion"] = (
        result.loc[tgt].groupby("motion")["opportunity_score"]
              .rank(pct=True).mul(100).round(1))

    # 57 names are shared by 123 firms -- distinct legal entities, sometimes in
    # different states with wildly different size (two "CAPITAL MANAGEMENT
    # ASSOCIATES INC"; five "RUSSELL INVESTMENTS"). Grouping a report by name
    # silently merges them, so give the BI layer a display field that is unique
    # by construction. Only duplicates are qualified, to keep the common case clean.
    dupes = result["name"].duplicated(keep=False)
    result["firm_display"] = result["name"].where(
        ~dupes,
        result["name"] + " (" + result["state"].fillna("--") + ", CRD "
        + result["crd"].astype(str) + ")")
    return result.sort_values("opportunity_score", ascending=False)
