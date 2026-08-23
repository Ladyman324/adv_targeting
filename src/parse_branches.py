"""Extract every advisor branch-office location into a mapping fact table.

Why this exists: CrntEmp/@state is the FIRM's address, not the adviser's. All
25,413 Merrill representatives carry emp_state="NY"; their branch offices are
spread across every state. Any geographic analysis must use branch locations.
"""
from __future__ import annotations

import pathlib
import sys
import zipfile
import xml.etree.ElementTree as ET

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
import config

ROOT = pathlib.Path(__file__).parents[1]
RAW = ROOT / "data" / "raw"
INTERIM = ROOT / "data" / "interim"


def parse(zip_path: pathlib.Path) -> pd.DataFrame:
    rows = []
    z = zipfile.ZipFile(zip_path)
    members = sorted(z.namelist())

    for mi, member in enumerate(members, 1):
        with z.open(member) as f:
            for _, el in ET.iterparse(f, events=("end",)):
                if el.tag != "Indvl":
                    continue
                info = el.find("./Info")
                if info is None:
                    el.clear(); continue
                pk = info.get("indvlPK")
                for emp in el.findall("./CrntEmps/CrntEmp"):
                    firm = emp.get("orgPK")
                    for b in emp.findall("./BrnchOfLocs/BrnchOfLoc"):
                        st = b.get("state")
                        if not st:
                            continue
                        rows.append({
                            "advisor_crd": pk,
                            "firm_crd": firm,
                            "branch_street1": b.get("str1"),
                            "branch_street2": b.get("str2"),
                            "branch_city": b.get("city"),
                            "branch_state": st,
                            "branch_postal": b.get("postlCd"),
                            "branch_country": b.get("cntry"),
                        })
                el.clear()
        print(f"  [{mi:>2}/{len(members)}] {member:24s} branches={len(rows):>9,}")
    return pd.DataFrame(rows)


def main() -> None:
    src = config.newest_feed(RAW, "IA_INDVL_Feed_*.xml.zip")
    print(f"parsing branches from {src.name} ...")
    br = parse(src)
    br = br.drop_duplicates()
    INTERIM.mkdir(parents=True, exist_ok=True)
    br.to_parquet(INTERIM / "advisor_branches.parquet", index=False)
    print(f"\nadvisor_branches {len(br):,} rows -> advisor_branches.parquet")
    print(f"  distinct advisors {br.advisor_crd.nunique():,}")
    print(f"  distinct states   {br.branch_state.nunique():,}")


if __name__ == "__main__":
    main()
