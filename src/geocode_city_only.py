"""Give city-only branches a location, so they can compete for a placement.

WHY THIS EXISTS
123,931 of 660,244 branch records name a city and state but no street. The
geocoder needs a street, so every one of them was dropped: branch_geocoded_*
holds 536,313 rows, exactly the street-bearing ones. They were not
de-prioritised, they were absent -- which means placement.py never saw them and
could not choose them however good the evidence.

That is not a rounding error, it is a systematic bias toward head offices.
Keith Telesca (CRD 4223682) is filed by EIC at two addresses: "Villanova, PA"
with no street, and EIC's Atlanta headquarters with one. The Villanova row --
the firm's own statement about where he works -- never reached the pipeline, so
he was placed at headquarters 700 miles away and marked confirmed. 31,514
advisors have a city-only branch in a state no street branch covers.

WHAT A CITY CENTROID IS AND IS NOT
A centroid is not an address. It says "this person works in this town", which
for a salesperson is the difference between the right territory and the wrong
one -- but it is not somewhere to drive. Rows written here are marked
city_level so downstream can type them as such and never present them as a
street address.

Centroids come from geo_index.json, which is built from addresses we already
geocoded, so every point is a place advisers demonstrably work rather than a
gazetteer guess. That covers 93.1% of city-only rows; the remaining long tail
of small towns stays unplaced, as it is today.

Writes data/interim/branch_city_level.parquet.
"""
from __future__ import annotations

import json
import pathlib

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
INTERIM = ROOT / "data" / "interim"
WEB = ROOT / "webapp" / "data"


def city_centroids() -> dict[tuple[str, str], tuple[float, float]]:
    """(CITY, ST) -> (lat, lon), from the cities index the webapp already ships."""
    index = json.loads((WEB / "geo_index.json").read_text(encoding="utf-8"))
    out = {}
    for city, entries in index["cities"].items():
        for state, lat, lon, *_ in entries:
            out[(city.upper().strip(), str(state).upper().strip())] = (lat, lon)
    return out


def main() -> None:
    branches = pd.read_parquet(ROOT / "data" / "output" / "advisor_branches.parquet")
    branches["advisor_crd"] = branches["advisor_crd"].astype(str)
    branches["firm_crd"] = branches["firm_crd"].astype(str)

    street = branches["branch_street1"].fillna("").astype(str).str.strip()
    city_only = branches[street == ""].dropna(
        subset=["branch_city", "branch_state"]).copy()

    city_only["city_u"] = city_only["branch_city"].str.upper().str.strip()
    city_only["state_u"] = city_only["branch_state"].str.upper().str.strip()

    centroids = city_centroids()
    located = [centroids.get((c, s)) for c, s in
               zip(city_only["city_u"], city_only["state_u"])]
    city_only["lat"] = [p[0] if p else None for p in located]
    city_only["lon"] = [p[1] if p else None for p in located]

    found = city_only[city_only["lat"].notna()].copy()

    # Deliberately NOT the same addr_key shape as a street row. Placement keys
    # on street|city|postal, which for these rows collapses to "|SPRINGFIELD|"
    # -- identical for Illinois, Massachusetts and Missouri. The state goes in
    # the postal slot so the key stays unique; it is never displayed.
    found["branch_street1"] = None
    found["branch_street2"] = None
    found["branch_postal"] = None
    found["branch_country"] = "United States"
    # Named city_addr_key, not addr_key: branch_geocoded_* already carries an
    # addr_key in a different four-part shape, and a same-named column would
    # be silently overwritten when the two are concatenated.
    found["city_addr_key"] = "|" + found["city_u"] + "|" + found["state_u"]
    found["city_level"] = True
    found["geocode_precision"] = "city"
    found["geocode_source"] = "city_centroid"
    found["motion"] = None            # filled from firms.parquet downstream

    # Carried so export_geojson can build a display name and firm label without
    # a sibling street row to copy from -- 14,909 of these advisors have none,
    # having never appeared on the map at all.
    people = pd.read_parquet(ROOT / "data" / "interim" / "advisors.parquet",
                             columns=["advisor_crd", "first_name", "last_name"])
    people["advisor_crd"] = people["advisor_crd"].astype(str)
    found = found.merge(people, on="advisor_crd", how="left")

    firms = pd.read_parquet(ROOT / "data" / "output" / "firms.parquet",
                            columns=["crd", "name"])
    firms["crd"] = firms["crd"].astype(str)
    found = found.merge(firms.rename(columns={"crd": "firm_crd", "name": "firm_display"}),
                        on="firm_crd", how="left")

    keep = ["advisor_crd", "firm_crd", "branch_street1", "branch_street2",
            "branch_city", "branch_state", "branch_postal", "branch_country",
            "city_addr_key", "lat", "lon", "city_level", "geocode_precision",
            "geocode_source", "motion", "first_name", "last_name", "firm_display"]
    found[keep].to_parquet(INTERIM / "branch_city_level.parquet", index=False)

    total = len(city_only)
    print(f"branch_city_level.parquet  {len(found):,} of {total:,} city-only rows "
          f"located ({len(found) / max(1, total) * 100:.1f}%)")
    print(f"  distinct advisors        : {found['advisor_crd'].nunique():,}")
    print(f"  distinct city/state      : {found.groupby(['city_u', 'state_u']).ngroups:,}")
    missing = city_only[city_only["lat"].isna()]
    print(f"  no centroid available    : {len(missing):,} rows across "
          f"{missing.groupby(['city_u', 'state_u']).ngroups:,} towns -- left unplaced")


if __name__ == "__main__":
    main()
