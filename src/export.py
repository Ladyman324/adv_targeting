"""Filter every table to the roster universe and export to data/output/.

One place that decides what ships, so a table can never be quietly left behind.
"""
from __future__ import annotations

import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))
import config  # noqa: E402

# name -> (source parquet, key to filter on)
TABLES = {
    "advisors":                    ("advisors.parquet", "advisor_crd"),
    "advisor_employments":         ("advisor_employments.parquet", "both"),
    "advisor_branches":            ("advisor_branches.parquet", "both"),
    "advisor_prior_registrations": ("advisor_prior_registrations.parquet", "advisor_crd"),
    "advisor_employment_history":  ("advisor_employment_history.parquet", "advisor_crd"),
    "advisor_exams":               ("advisor_exams.parquet", "advisor_crd"),
    "advisor_other_business":      ("advisor_other_business.parquet", "advisor_crd"),
    "advisor_other_names":         ("advisor_other_names.parquet", "advisor_crd"),
    "sec_nickname_evidence":       ("sec_nickname_evidence.parquet", "unfiltered"),
    "firm_state_registrations":    ("firm_state_registrations.parquet", "firm_crd"),
    "firm_websites":               ("firm_websites.parquet", "firm_crd"),
    "firm_other_names":            ("firm_other_names.parquet", "firm_crd"),
}


def main() -> None:
    firms = pd.read_parquet(config.INTERIM / "firms_scored.parquet")
    firm_ids = set(firms["crd"].astype(str))

    # Advisor universe = those with an employment at a roster firm.
    emp = pd.read_parquet(config.INTERIM / "advisor_employments.parquet")
    emp["firm_crd"] = emp["firm_crd"].astype(str)
    emp = emp[emp["firm_crd"].isin(firm_ids)]
    advisor_ids = set(emp["advisor_crd"])

    config.OUTPUT.mkdir(parents=True, exist_ok=True)
    firms.to_parquet(config.OUTPUT / "firms.parquet", index=False)
    firms.to_csv(config.OUTPUT / "firms.csv", index=False)
    print(f"  {'firms':30s} {len(firms):>9,} rows x {firms.shape[1]:>4} cols")

    for name, (fname, key) in TABLES.items():
        src = config.INTERIM / fname
        if not src.exists():
            print(f"  {name:30s} -- MISSING {fname}")
            continue
        df = pd.read_parquet(src)
        if "firm_crd" in df:
            df["firm_crd"] = df["firm_crd"].astype(str)
        if key in ("firm_crd", "both") and "firm_crd" in df:
            df = df[df["firm_crd"].isin(firm_ids)]
        if key in ("advisor_crd", "both") and "advisor_crd" in df:
            df = df[df["advisor_crd"].isin(advisor_ids)]
        df.to_parquet(config.OUTPUT / f"{name}.parquet", index=False)
        print(f"  {name:30s} {len(df):>9,} rows x {df.shape[1]:>4} cols")


if __name__ == "__main__":
    main()
