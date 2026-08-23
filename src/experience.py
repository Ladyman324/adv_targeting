"""Derive advisor tenure from the date of their first securities exam.

Why the first exam and not employment history: `advisor_employment_history`
from_dates are unusable as a career-start proxy -- they cliff hard at ~2016
(one record reads 04/1892) and taking min(exam, employment) dragged every
advisor's start backwards, producing a floor of 6 years and no advisor
registered after 2020. First exam is 97.7% covered, runs cleanly 1950->2026,
and is semantically the start of a securities career (S63/S66/S65 dominate).

NOTE: this is tenure in the industry, not age. Form ADV / IAPD publishes no
date of birth, so advisor age is not derivable from this data at all.
"""
from __future__ import annotations

import pathlib

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
OUTPUT = ROOT / "data" / "output"

AS_OF = pd.Timestamp.today().normalize()
MIN_YEAR = 1950          # below this the source date is a data-entry error


def build() -> pd.DataFrame:
    x = pd.read_parquet(OUTPUT / "advisor_exams.parquet")
    x["d"] = pd.to_datetime(x["exam_date"], errors="coerce")
    x = x[x["d"].dt.year >= MIN_YEAR]

    idx = x.groupby("advisor_crd")["d"].idxmin()
    first = x.loc[idx, ["advisor_crd", "d", "exam_code", "exam_name"]]
    first = first.rename(columns={"d": "first_exam_date",
                                  "exam_code": "first_exam_code",
                                  "exam_name": "first_exam_name"})
    first["years_experience"] = ((AS_OF - first["first_exam_date"]).dt.days / 365.25).round(1)
    first["experience_band"] = pd.cut(
        first["years_experience"], [-0.01, 5, 10, 20, 30, 200],
        labels=["<5", "5-10", "10-20", "20-30", "30+"]).astype(str)
    return first.reset_index(drop=True)


def main() -> None:
    df = build()
    df.to_parquet(OUTPUT / "advisor_experience.parquet", index=False)
    print(f"advisor_experience {len(df):,} rows -> advisor_experience.parquet")
    print(f"  coverage {len(df)/414784:.1%} of advisors")
    print(f"  median {df['years_experience'].median():.1f} yrs | "
          f"mean {df['years_experience'].mean():.1f} | max {df['years_experience'].max():.1f}")
    print(df["experience_band"].value_counts().reindex(["<5","5-10","10-20","20-30","30+"]).to_string())


if __name__ == "__main__":
    main()
