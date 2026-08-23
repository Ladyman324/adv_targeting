"""Allocate firm-level RAUM down to advisor branch locations.

Form ADV reports assets at the firm level only -- there is no state breakdown.
Any state AUM figure is therefore an ESTIMATE resting on one assumption:

    every advisor at a firm manages an equal share of that firm's assets.

That is certainly false at the individual level (a 30-year veteran does not
manage the same book as a first-year associate) but it is the only allocation
the data supports, and it is unbiased in aggregate across many firms.

The split is done twice so totals reconcile exactly:
  1. firm RAUM / advisors at that firm      -> AUM per advisor
  2. AUM per advisor / that advisor's branches -> AUM per branch row

Step 2 matters: without it, an advisor registered in two states would count
their full book in both, inflating the national total.
"""
from __future__ import annotations

import pathlib

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
OUT = ROOT / "data" / "output"


def main() -> None:
    firms = pd.read_parquet(OUT / "firms.parquet")
    emp = pd.read_parquet(OUT / "advisor_employments.parquet")
    br = pd.read_parquet(OUT / "advisor_branches.parquet")

    # Advisors per firm counted from the BRANCH table, not the employment
    # bridge. A small number of advisers have an employment record but no
    # branch location; counting them in the denominator while they contribute
    # no row would silently leak their share of AUM (~$179B). Using the
    # placeable population instead redistributes it across their colleagues at
    # the same firm, and makes the allocation reconcile exactly.
    per_firm = (br.groupby("firm_crd")["advisor_crd"].nunique()
                .rename("firm_advisors"))
    # branch rows per (advisor, firm) pair
    per_pair = (br.groupby(["firm_crd", "advisor_crd"]).size()
                .rename("pair_branches"))

    b = (br.join(per_firm, on="firm_crd")
           .join(per_pair, on=["firm_crd", "advisor_crd"])
           .merge(firms[["crd", "raum_total"]], left_on="firm_crd",
                  right_on="crd", how="left")
           .drop(columns="crd"))

    b["aum_per_advisor"] = b["raum_total"] / b["firm_advisors"]
    b["aum_allocated"] = b["aum_per_advisor"] / b["pair_branches"]
    b = b.drop(columns=["raum_total", "aum_per_advisor"])

    b.to_parquet(OUT / "advisor_branches.parquet", index=False)

    # --- reconciliation -------------------------------------------------
    allocated = b["aum_allocated"].sum()
    firms_with_reps = set(b["firm_crd"])
    reconcilable = firms.loc[firms["crd"].isin(firms_with_reps), "raum_total"].sum()
    total = firms["raum_total"].sum()

    print(f"rows                       {len(b):>16,}")
    print(f"allocated AUM              ${allocated:>16,.0f}")
    print(f"RAUM of firms with reps    ${reconcilable:>16,.0f}")
    print(f"difference                 ${allocated - reconcilable:>16,.0f}")
    print(f"total roster RAUM          ${total:>16,.0f}")
    print(f"unallocated (firms w/o reps) ${total - reconcilable:>14,.0f} "
          f"({(total - reconcilable) / total:.1%})")


if __name__ == "__main__":
    main()
