"""Extract each firm's state-by-state notice filings from the firm XML feed.

The roster CSV collapses this to a single "Jurisdiction Notice Filed-Effective
Date". The XML carries one row per state with its own regulator code, status
and date -- i.e. the firm's actual registration footprint, which is what
territory work needs.
"""
from __future__ import annotations

import gzip
import pathlib
import sys
import xml.etree.ElementTree as ET

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
import config

ROOT = pathlib.Path(__file__).parents[1]
RAW = ROOT / "data" / "raw"
INTERIM = ROOT / "data" / "interim"


def parse() -> pd.DataFrame:
    src = config.newest_feed(RAW, "IA_FIRM_SEC_Feed_*.xml.gz")
    rows = []
    for _, el in ET.iterparse(gzip.open(src, "rb"), events=("end",)):
        if el.tag != "Firm":
            continue
        info = el.find("./Info")
        if info is not None:
            crd = info.get("FirmCrdNb")
            for st in el.findall("./NoticeFiled/States"):
                rows.append({
                    "firm_crd": crd,
                    "regulator_code": st.get("RgltrCd"),
                    "status": st.get("St"),
                    "effective_date": st.get("Dt"),
                })
        el.clear()
    return pd.DataFrame(rows)


def main() -> None:
    df = parse().drop_duplicates()
    INTERIM.mkdir(parents=True, exist_ok=True)
    df.to_parquet(INTERIM / "firm_state_registrations.parquet", index=False)
    print(f"firm_state_registrations {len(df):,} rows")
    print(f"  firms covered  {df.firm_crd.nunique():,}")
    print(f"  jurisdictions  {df.regulator_code.nunique()}")
    print(f"  median states per firm  {df.groupby('firm_crd').size().median():.0f}")


if __name__ == "__main__":
    main()
