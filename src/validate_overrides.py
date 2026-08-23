"""Grade each override row so a street-centroid guess is never mistaken for a rooftop.

Nominatim answers a query it cannot place exactly by returning the STREET, not
the building -- "900 SALEM STREET" comes back as "Salem Street, Smithfield".
That is usable for a map but it is not the address, and the distinction has to
survive into the data rather than being flattened to "we found it".

Two checks, both purely textual against the stored match:
  house number present in the match  -> rooftop, else approximate
  filed city present in the match    -> else CITY MISMATCH, sent for manual review

Rows failing the city check are blanked, not kept with a warning: a coordinate
in the wrong town is worse than no coordinate.
"""
from __future__ import annotations

import pathlib
import re

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
OVERRIDES = ROOT / "data" / "reference" / "address_overrides.csv"

# city aliases the two sources spell differently
ALIAS = {
    "NEW YORK": ["NEW YORK", "MANHATTAN", "BROOKLYN", "QUEENS"],
    "ST. LOUIS": ["ST. LOUIS", "SAINT LOUIS"],
    "ST LOUIS": ["ST. LOUIS", "SAINT LOUIS"],
}


def house_number(street: str) -> str:
    m = re.match(r"^\s*(\d+)\b", str(street))
    return m.group(1) if m else ""


def main() -> None:
    df = pd.read_csv(OVERRIDES, dtype=str).fillna("")
    got = df["lat"].ne("")

    prec, check = [], []
    for _, r in df.iterrows():
        if not r["lat"]:
            prec.append("")
            check.append("manual lookup needed")
            continue
        name = r["matched_name"].upper()
        city = r["city"].upper().strip()
        cands = ALIAS.get(city, [city])
        city_ok = any(c in name for c in cands)
        hn = house_number(r["street"])
        # the match is the building only if the filed house number came back
        exact = bool(hn) and re.search(rf"\b{hn}\b", name) is not None
        if not city_ok:
            prec.append("")
            check.append("CITY MISMATCH -- blanked, needs manual lookup")
        else:
            prec.append("rooftop" if exact else "approximate")
            check.append("ok" if exact else "street-level match, not the building")

    df["precision"] = prec
    df["check"] = check
    # a coordinate in the wrong town is worse than none
    bad = df["check"].str.startswith("CITY MISMATCH")
    df.loc[bad, ["lat", "lon", "source", "verified_on"]] = ""

    df.to_csv(OVERRIDES, index=False)
    df["pins"] = df["pins"].astype(int)

    print(f"{len(df)} override rows\n")
    for k, g in df.groupby(df["check"].str.split(" --").str[0]):
        print(f"  {k:42s} {len(g):>3} rows  {g['pins'].sum():>6,} pins")

    usable = df[df["lat"].ne("")]
    print(f"\nusable now: {len(usable)} rows, {usable['pins'].sum():,} pins "
          f"(rooftop {(usable['precision']=='rooftop').sum()}, "
          f"approximate {(usable['precision']=='approximate').sum()})")
    todo = df[df["lat"].eq("")]
    print(f"manual:     {len(todo)} rows, {todo['pins'].sum():,} pins")
    if len(todo):
        print()
        for _, r in todo.sort_values("pins", ascending=False).iterrows():
            print(f"  {int(r['pins']):>5,}  {r['state']}  {r['street'][:36]:36.36s} "
                  f"{r['city'][:16]:16.16s} {r['zip']:6s} {r['firm'][:28]}")


if __name__ == "__main__":
    main()
