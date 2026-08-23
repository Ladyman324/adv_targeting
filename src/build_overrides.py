"""Resolve the highest-cost unplaced addresses into a reviewable override table.

The Census TIGER file is a *street range* database. It cannot place an address
that is not on a public street range -- corporate campuses on private roads
("9800 Schwab Way"), named towers with no street number ("One Bryant Park",
"The William Blair Building"), or filed ranges ("1100-1800 American Blvd").
These are a small number of addresses carrying a large number of advisors, so
they are worth resolving individually rather than statistically.

Nominatim (OpenStreetMap) is tried first because OSM carries named buildings
and private campus roads that TIGER omits. Anything it cannot place is written
out for manual lookup -- nothing is invented here.

Every row records where the coordinate came from and when, so the table can be
audited later. Output is a CSV meant to be read and corrected by a human.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import time

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).parents[1]
INTERIM = ROOT / "data" / "interim"
REF = ROOT / "data" / "reference"
OVERRIDES = REF / "address_overrides.csv"

NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy: <=1 request/second and a real identifying UA.
UA = "EIC-adv-targeting/1.0 (internal sales research; bladyman@eicatlanta.com)"
PAUSE = 1.1

COLS = ["state", "street", "city", "zip", "lat", "lon", "source", "verified_on",
        "matched_name", "pins", "firm", "note"]


def unplaced(limit: int) -> pd.DataFrame:
    """The addresses that cost the most advisor pins, worst first."""
    rows = []
    for f in sorted(INTERIM.glob("branch_geocoded_*.parquet")):
        st = f.stem.replace("branch_geocoded_", "")
        d = pd.read_parquet(f, columns=["advisor_crd", "lat", "branch_street1",
                                        "branch_city", "branch_postal", "firm_display"])
        bad = d[d["lat"].isna()].copy()
        if not len(bad):
            continue
        bad["street"] = bad["branch_street1"].astype(str).str.strip()
        bad["city"] = bad["branch_city"].astype(str).str.strip()
        bad["zip"] = bad["branch_postal"].astype(str).str[:5]
        g = (bad.groupby(["street", "city", "zip"])
                .agg(pins=("advisor_crd", "size"),
                     firm=("firm_display", lambda s: s.mode().iloc[0] if len(s.mode()) else ""))
                .reset_index())
        g["state"] = st
        rows.append(g)
    a = pd.concat(rows, ignore_index=True).sort_values("pins", ascending=False)
    return a.head(limit).reset_index(drop=True)


def ask_nominatim(street: str, city: str, state: str, zip5: str) -> dict | None:
    """Structured query first, then a free-text one. Returns None if unsure."""
    attempts = [
        {"street": street, "city": city, "state": state, "postalcode": zip5,
         "country": "USA", "format": "jsonv2", "limit": 1},
        {"q": f"{street}, {city}, {state} {zip5}, USA", "format": "jsonv2", "limit": 1},
    ]
    for params in attempts:
        try:
            r = requests.get(NOMINATIM, params=params,
                             headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            hits = r.json()
        except Exception as e:
            print(f"      nominatim error: {type(e).__name__}")
            time.sleep(PAUSE)
            continue
        time.sleep(PAUSE)
        if hits:
            h = hits[0]
            return {"lat": float(h["lat"]), "lon": float(h["lon"]),
                    "matched_name": h.get("display_name", "")[:120]}
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50, help="how many addresses to attempt")
    args = ap.parse_args()

    REF.mkdir(parents=True, exist_ok=True)
    todo = unplaced(args.limit)
    print(f"attempting the {len(todo)} unplaced addresses that cost the most pins "
          f"({todo['pins'].sum():,} pins)\n")

    out = []
    for i, r in todo.iterrows():
        got = ask_nominatim(r["street"], r["city"], r["state"], r["zip"])
        rec = {"state": r["state"], "street": r["street"], "city": r["city"],
               "zip": r["zip"], "pins": int(r["pins"]), "firm": r["firm"],
               "lat": "", "lon": "", "source": "", "verified_on": "",
               "matched_name": "", "note": ""}
        if got:
            rec.update(lat=round(got["lat"], 6), lon=round(got["lon"], 6),
                       source="nominatim", verified_on=dt.date.today().isoformat(),
                       matched_name=got["matched_name"])
            flag = "ok"
        else:
            rec["note"] = "NEEDS MANUAL LOOKUP"
            flag = "--"
        out.append(rec)
        print(f"  [{i+1:>2}/{len(todo)}] {flag}  {r['pins']:>5,}  {r['state']}  "
              f"{r['street'][:34]:34.34s} {r['city'][:16]:16.16s} "
              f"{rec['matched_name'][:44]}")

    df = pd.DataFrame(out)[COLS]
    df.to_csv(OVERRIDES, index=False)
    hit = df["lat"].astype(str).ne("").sum()
    print(f"\nresolved {hit}/{len(df)} -> {OVERRIDES}")
    print(f"pins recoverable now: {df.loc[df['lat'].astype(str).ne(''), 'pins'].sum():,}")
    print(f"pins still needing manual lookup: "
          f"{df.loc[df['lat'].astype(str).eq(''), 'pins'].sum():,}")


if __name__ == "__main__":
    main()
