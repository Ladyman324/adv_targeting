"""Cetera advisor roster -> data/raw/firm_rosters/cetera_<date>.csv

Cetera publishes a plain JSON endpoint behind its Find an Advisor page:

    GET https://cetera.com/api/advisor/location?address=<lat,lng>&distance=<miles>

It is the best of the seven sources we scrape, because every record carries the
advisor's own CRD. No name matching is needed for Cetera -- the join to the SEC
data is exact.

WHY THE QUERY POINTS ARE NOT A COARSE GRID
------------------------------------------
The obvious design -- eleven 900-mile circles over the US -- does not work. The
endpoint returns HTTP 200 with a literal `[]` for some (point, radius) pairs and
gives no indication that anything went wrong. Measured, on the eleven-point
900-mile grid this file used to carry:

    (32.0,-100.0) west Texas   -> 0     (also 0 at 100/200/300/400/600 miles)
    (42.0,-115.0) Nevada       -> 0     (but 115 results at 300 miles)
    (45.0,-93.0)  Minneapolis  -> 0     (but 763 results at 300 miles)
    (21.3,-157.8) Honolulu     -> 0     (but 80 results at 100 miles)

Four of eleven silently empty, and an empty response is indistinguishable from
"nobody here". A scraper that cannot tell those apart reports a number it has
not earned.

So instead of covering the country, we cover the ADVISORS: the seed points are
the geocoded branch cities of CRD 105644 from our own SEC extract, thinned to a
MESH_MILES mesh and queried at RADIUS_MILES. That guarantees every place Cetera
actually files an office is inside a query circle, keeps the radius in the range
that answers reliably, and -- because we know how many advisors the SEC says
Cetera has -- gives a completeness figure at the end rather than a guess.

Run:  python src/cetera_async.py [--mesh 60] [--radius 100] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import random
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path, scratch_path  # one naming convention, defined once

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
GEO_INDEX = ROOT / "webapp" / "data" / "geo_index.json"
BRANCHES = ROOT / "data" / "output" / "advisor_branches.parquet"

API_URL = "https://cetera.com/api/advisor/location"
CETERA_CRD = "105644"
HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "referer": "https://cetera.com/find-an-advisor",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
}

RADIUS_MILES = 100   # answers reliably; 150+ is where empty responses start
MESH_MILES = 60      # seed spacing, comfortably inside the radius
PAUSE = 1.0
RETRIES = 3


def miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d = (math.sin(p1) * math.sin(p2)
         + math.cos(p1) * math.cos(p2) * math.cos(math.radians(lon1 - lon2)))
    return 3959 * math.acos(max(-1.0, min(1.0, d)))


def seed_points(mesh: float) -> list[tuple[float, float]]:
    """Cetera's own branch cities, thinned so no two seeds are within `mesh`."""
    if not GEO_INDEX.exists() or not BRANCHES.exists():
        raise SystemExit(f"need {GEO_INDEX} and {BRANCHES}; run the ADV pipeline first")

    geo = json.loads(GEO_INDEX.read_text(encoding="utf-8"))
    zips, cities = geo["zips"], geo["cities"]

    branches = pd.read_parquet(
        BRANCHES, columns=["firm_crd", "branch_city", "branch_state", "branch_postal"])
    branches = branches[branches["firm_crd"].astype(str) == CETERA_CRD]

    located, unresolved = set(), 0
    for row in branches.itertuples():
        zip5 = str(row.branch_postal or "")[:5]
        if zip5 in zips:                       # zip centroid where we have one
            located.add((zips[zip5][1], zips[zip5][2]))
            continue
        entry = cities.get(str(row.branch_city or "").upper()) or []
        hit = [e for e in entry if e[0] == row.branch_state]
        if hit:                                # else the city centroid in that state
            located.add((hit[0][1], hit[0][2]))
        else:
            unresolved += 1

    seeds: list[tuple[float, float]] = []
    for point in sorted(located):
        if all(miles(*point, *s) > mesh for s in seeds):
            seeds.append(point)
    print(f"[*] {len(branches):,} Cetera branch rows -> {len(located):,} distinct places "
          f"-> {len(seeds)} query seeds at a {mesh:.0f}-mile mesh "
          f"({unresolved} rows had no centroid)")
    return seeds


def fetch(session: requests.Session, lat: float, lng: float, radius: float):
    """One query. Returns (rows, ok) -- ok is False when every attempt errored,
    which is NOT the same as a successful empty response."""
    for attempt in range(RETRIES):
        try:
            r = session.get(API_URL, headers=HEADERS, timeout=90,
                            params={"address": f"{lat},{lng}", "distance": radius})
            if r.status_code == 200:
                payload = r.json()
                return (payload if isinstance(payload, list) else []), True
            print(f"    [-] HTTP {r.status_code} at ({lat}, {lng})")
        except Exception as exc:
            print(f"    [-] {type(exc).__name__} at ({lat}, {lng}): {exc}")
        time.sleep(PAUSE * (2 ** attempt) + random.random())
    return [], False


COLUMNS = ["advisor_crd", "advisor_id", "name", "first_name", "middle_name",
           "last_name", "pref_name", "address", "address2", "city", "state",
           "postal", "phone", "lat", "lon", "branch", "field_advisor"]


def normalise(rec: dict) -> dict:
    """API record -> our column names. `miles` is dropped: it is the distance to
    whichever seed happened to find the advisor, so it means nothing once the
    circles are merged."""
    return {
        "advisor_crd": (rec.get("crd") or "").strip(),
        "advisor_id": rec.get("id", ""),
        "name": (rec.get("name") or "").strip(),
        "first_name": rec.get("first_name", ""),
        "middle_name": rec.get("middle_name", ""),
        "last_name": rec.get("last_name", ""),
        "pref_name": rec.get("pref_name", ""),
        "address": rec.get("address", ""),
        "address2": rec.get("address2", ""),
        "city": rec.get("city", ""),
        "state": rec.get("STATE", "") or rec.get("state", ""),   # the API shouts this one
        "postal": rec.get("postal", ""),
        "phone": rec.get("phone", ""),
        "lat": rec.get("lat", ""),
        "lon": rec.get("lng", ""),
        "branch": rec.get("branch", ""),
        "field_advisor": rec.get("field_advisor", ""),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mesh", type=float, default=MESH_MILES)
    ap.add_argument("--radius", type=float, default=RADIUS_MILES)
    ap.add_argument("--dry-run", action="store_true", help="plan the seeds, query nothing")
    args = ap.parse_args()

    seeds = seed_points(args.mesh)
    if args.dry_run:
        print(f"[*] dry run: would issue {len(seeds)} requests at {args.radius:.0f} miles")
        return

    started = time.time()
    records, failures, empties = {}, [], 0
    session = requests.Session()

    for i, (lat, lng) in enumerate(seeds, 1):
        rows, ok = fetch(session, lat, lng, args.radius)
        if not ok:
            failures.append((lat, lng))
        elif not rows:
            empties += 1
        new = 0
        for rec in rows:
            key = rec.get("id") or rec.get("crd")
            if key and key not in records:
                records[key] = rec
                new += 1
        print(f"[{i}/{len(seeds)}] ({lat:.2f}, {lng:.2f})  {len(rows):>4} rows, "
              f"{new:>4} new  |  total {len(records):,}")
        time.sleep(PAUSE)

    if not records:
        raise SystemExit("no advisors collected -- refusing to write an empty roster")

    # the untouched payload goes to interim/, not next to the finished rosters
    raw = scratch_path("cetera", "api", ext="json")
    raw.write_text(json.dumps(list(records.values()), indent=1), encoding="utf-8")

    df = pd.DataFrame([normalise(r) for r in records.values()], columns=COLUMNS)
    out = roster_path("cetera")
    df.to_csv(out, index=False)

    with_crd = int((df["advisor_crd"] != "").sum())
    print(f"\n[*] {len(df):,} advisors in {time.time() - started:.0f}s -> {out}")
    print(f"    raw payload -> {raw}")
    print(f"    {with_crd:,} carry a CRD ({with_crd / len(df):.1%}) -- these join exactly")

    # Coverage is an INTERSECTION of CRDs, never a ratio of totals. Those two
    # numbers can differ by a lot and the ratio always flatters: this scrape is
    # 93.7% of Cetera's advisor COUNT but only 75.9% of its advisor IDENTITIES,
    # because ~1,400 scraped reps are not in the ADV extract at all.
    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        b["advisor_crd"] = b["advisor_crd"].astype(str)
        sec = set(b[b["firm_crd"].astype(str) == CETERA_CRD]["advisor_crd"])
        everyone = set(b["advisor_crd"])
        got = set(df.loc[df["advisor_crd"] != "", "advisor_crd"].str.strip())
        print(f"    of {len(sec):,} SEC advisors at CRD {CETERA_CRD}, matched "
              f"{len(got & sec):,} ({len(got & sec) / len(sec):.1%}); "
              f"{len(sec - got):,} not found by the scrape")
        print(f"    {len(got & everyone - sec):,} scraped CRDs sit at a different firm "
              f"in ADV; {len(got - everyone):,} are absent from ADV entirely "
              f"(brokerage-only reps)")
    if empties:
        print(f"    {empties} seed(s) returned an empty list -- expected in sparse areas, "
              f"but check the count against the coverage figure above")
    if failures:
        print(f"    {len(failures)} seed(s) FAILED after {RETRIES} attempts: {failures}")


if __name__ == "__main__":
    main()
