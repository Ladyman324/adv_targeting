"""Apply the hand-curated override table to the geocoded state parquets.

The override table exists because the Census TIGER file is a street-range
database and cannot place a named tower or a private campus road. These are
few in number but carry a lot of advisors, so they are resolved individually
and recorded with their source.

Rules:
  * only ever FILLS a null coordinate -- a Census result is never overwritten
  * precision comes from the table, so a POI or street-centroid match can
    never be read back as a rooftop one
  * geocode_source records where each coordinate came from, so the table can
    be re-derived or replaced later without guessing what is in the data

Run after geocode.py / run_national.py:
    python src\\apply_overrides.py            # dry run
    python src\\apply_overrides.py --apply    # write parquets + GeoJSON
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
import export_geojson  # noqa: E402

INTERIM = ROOT / "data" / "interim"
OVERRIDES = ROOT / "data" / "reference" / "address_overrides.csv"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry run")
    args = ap.parse_args()

    if not OVERRIDES.exists():
        sys.exit(f"no override table at {OVERRIDES} -- run build_overrides.py first")

    ov = pd.read_csv(OVERRIDES, dtype=str).fillna("")
    ov = ov[ov["lat"].ne("")]
    if ov.empty:
        sys.exit("override table has no resolved rows")
    print(f"{len(ov)} resolved overrides\n")

    total_pins = 0
    report = []
    for st, grp in ov.groupby("state"):
        f = INTERIM / f"branch_geocoded_{st}.parquet"
        if not f.exists():
            print(f"  !! {st}: no parquet")
            continue
        d = pd.read_parquet(f)
        if "geocode_source" not in d.columns:
            d["geocode_source"] = pd.Series(
                ["census" if x else None for x in d["lat"].notna()], index=d.index)

        # Match on filed street + city, CASE-INSENSITIVELY. Matching the exact
        # recorded casing stranded 34 addresses / 489 pins: 5525 NW Fisher Creek
        # Drive is filed three ways in the Camas parquet -- upper, mixed, and
        # city "camas" -- so an override recorded in one casing reached only its
        # own variant and left the rest null. Nothing distinct differs by case
        # alone, so folding it can only merge what belongs together.
        key_d = (d["branch_street1"].astype(str).str.strip().str.upper() + "|"
                 + d["branch_city"].astype(str).str.strip().str.upper())
        filled = 0
        for _, r in grp.iterrows():
            k = f"{r['street']}|{r['city']}".upper()
            m = key_d.eq(k) & d["lat"].isna()
            if not m.any():
                continue
            d.loc[m, "lat"] = float(r["lat"])
            d.loc[m, "lon"] = float(r["lon"])
            d.loc[m, "geocode_precision"] = r["precision"] or "approximate"
            d.loc[m, "geocode_source"] = r["source"]
            d.loc[m, "matched"] = r["matched_name"]
            filled += int(m.sum())

        if not filled:
            continue
        total_pins += filled
        report.append({"state": st, "overrides": len(grp), "pins": filled})
        if args.apply:
            d.to_parquet(f, index=False)
            export_geojson.export(st)

    r = pd.DataFrame(report).sort_values("pins", ascending=False)
    print(r.to_string(index=False))
    print(f"\n{'APPLIED' if args.apply else 'DRY RUN'}: +{total_pins:,} pins "
          f"across {len(report)} states")
    if not args.apply:
        print("re-run with --apply to write")


if __name__ == "__main__":
    main()
