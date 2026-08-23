"""Robert W. Baird advisor roster -> data/raw/firm_rosters/baird_<date>.csv

    GET https://www.bairdwealth.com/api/advisor/search
        ?query=<lat,lng|zip>&radius=<miles>&maxResults=<n>

The richest of the sources we scrape. Every advisor record carries a WORK EMAIL
ADDRESS, a direct phone, a title, a team name, an office name and that office's
manager. No other firm gives us email.

What it does NOT carry is a CRD, so unlike Cetera this one has to be matched on
name and geography. Name, office street address, city, state, and lat/lon all
come through clean, which is the strongest corroboration set we have had.

MEASURED LIMITS (probed, not assumed)
-------------------------------------
    maxResults   1000 is the ceiling; 1024 returns HTTP 400
    radius       500 is the ceiling; 501 returns HTTP 400
    truncation   a full page is silent -- Chicago at radius 500 returns exactly
                 1000 results and reports "totalResults": 1000. There is no flag
                 and no paging, so a full page must be treated as TRUNCATED and
                 subdivided, never as a complete answer.
    auto-widen   if the requested radius finds nothing, the server silently
                 re-runs at 500 miles and says so in metadata.radiusMiles. Rural
                 Kansas at radius 25 returns 220 advisors up to 488 miles away.
                 Harmless once results are deduped, but it means a response can
                 cover far more ground than was asked for -- so never infer
                 "these are the advisors within N miles" from the request alone.

Query points are Baird's own branch cities from our SEC extract for CRD 8158,
thinned to a MESH_MILES mesh -- the same approach as cetera_async.py, and for
the same reason: cover the advisors, not the map. A coarse national sweep at the
maximum radius then runs as a safety net, and reports anything it finds that the
mesh missed. If that number is not ~0 the mesh needs widening.

Run:  python src/baird_async.py [--mesh 60] [--radius 100] [--dry-run]
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

API_URL = "https://www.bairdwealth.com/api/advisor/search"
BAIRD_CRD = "8158"
HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "accept-language": "en-US,en;q=0.9",
    "x-requested-with": "XMLHttpRequest",
    "referer": "https://www.bairdwealth.com/",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
}

MAX_RESULTS = 1000   # server ceiling; 1024 is a 400
MAX_RADIUS = 500     # server ceiling; 501 is a 400
RADIUS_MILES = 100
MESH_MILES = 60
MIN_RADIUS = 12      # stop subdividing here and report, rather than loop forever
PAUSE = 0.6
RETRIES = 3

# Coarse national sweep at MAX_RADIUS. Only a safety net -- the mesh above does
# the real work -- so gaps between these circles are acceptable.
SWEEP = [(47.6, -122.3), (45.5, -111.0), (47.5, -97.0), (44.9, -93.1),
         (42.4, -83.0), (43.0, -76.1), (42.4, -71.1), (39.9, -75.2),
         (35.2, -80.8), (28.5, -81.4), (33.7, -84.4), (35.1, -90.0),
         (39.8, -89.6), (39.1, -94.6), (29.8, -95.4), (32.8, -96.8),
         (35.1, -106.6), (39.7, -105.0), (40.8, -111.9), (33.4, -112.1),
         (34.1, -118.2), (37.8, -122.4), (45.5, -122.7), (61.2, -149.9),
         (21.3, -157.8)]


def miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d = (math.sin(p1) * math.sin(p2)
         + math.cos(p1) * math.cos(p2) * math.cos(math.radians(lon1 - lon2)))
    return 3959 * math.acos(max(-1.0, min(1.0, d)))


def seed_points(mesh: float) -> list[tuple[float, float]]:
    """Baird's own branch cities, thinned so no two seeds are within `mesh`."""
    if not GEO_INDEX.exists() or not BRANCHES.exists():
        raise SystemExit(f"need {GEO_INDEX} and {BRANCHES}; run the ADV pipeline first")

    geo = json.loads(GEO_INDEX.read_text(encoding="utf-8"))
    zips, cities = geo["zips"], geo["cities"]
    branches = pd.read_parquet(
        BRANCHES, columns=["firm_crd", "branch_city", "branch_state", "branch_postal"])
    branches = branches[branches["firm_crd"].astype(str) == BAIRD_CRD]

    located, unresolved = set(), 0
    for row in branches.itertuples():
        zip5 = str(row.branch_postal or "")[:5]
        if zip5 in zips:
            located.add((zips[zip5][1], zips[zip5][2]))
            continue
        hit = [e for e in (cities.get(str(row.branch_city or "").upper()) or [])
               if e[0] == row.branch_state]
        if hit:
            located.add((hit[0][1], hit[0][2]))
        else:
            unresolved += 1

    seeds: list[tuple[float, float]] = []
    for point in sorted(located):
        if all(miles(*point, *s) > mesh for s in seeds):
            seeds.append(point)
    print(f"[*] {len(branches):,} Baird branch rows -> {len(located):,} distinct places "
          f"-> {len(seeds)} query seeds at a {mesh:.0f}-mile mesh "
          f"({unresolved} rows had no centroid)")
    return seeds


def fetch(session: requests.Session, lat: float, lng: float, radius: float):
    """One query. Returns (results, ok). `ok` is False only when every attempt
    errored -- which is a different fact from a successful empty result."""
    for attempt in range(RETRIES):
        try:
            r = session.get(API_URL, headers=HEADERS, timeout=90,
                            params={"query": f"{lat:.4f},{lng:.4f}",
                                    "radius": int(radius), "maxResults": MAX_RESULTS})
            if r.status_code == 200:
                payload = r.json()
                if payload.get("success"):
                    return payload.get("results") or [], True
                print(f"    [-] success=false at ({lat}, {lng}): "
                      f"{payload.get('errorMessage')}")
            else:
                print(f"    [-] HTTP {r.status_code} at ({lat}, {lng})")
        except Exception as exc:
            print(f"    [-] {type(exc).__name__} at ({lat}, {lng}): {exc}")
        time.sleep(PAUSE * (2 ** attempt) + random.random())
    return [], False


def collect(session, lat, lng, radius, sink, failures, truncated):
    """Query one circle, and split it if the page came back full.

    A full page means the server stopped early and told us nothing about what it
    dropped, so the only safe reading is 'there is more here'. Splitting into
    four half-radius circles offset by half a radius covers the same disc."""
    rows, ok = fetch(session, lat, lng, radius)
    if not ok:
        failures.append((lat, lng, radius))
        return 0
    new = 0
    for rec in rows:
        key = (rec.get("resultType"), rec.get("id"))
        if key[1] is not None and key not in sink:
            sink[key] = rec
            new += 1

    if len(rows) >= MAX_RESULTS:
        if radius <= MIN_RADIUS:
            truncated.append((lat, lng, radius))
            return new
        half = radius / 2
        offset = half / 69.0                       # degrees latitude per mile
        lon_offset = offset / max(0.2, math.cos(math.radians(lat)))
        print(f"    [!] full page at ({lat:.2f}, {lng:.2f}) r={radius:.0f} "
              f"-- splitting into 4 at r={half:.0f}")
        for dlat, dlon in ((+offset, +lon_offset), (+offset, -lon_offset),
                           (-offset, +lon_offset), (-offset, -lon_offset)):
            time.sleep(PAUSE)
            new += collect(session, lat + dlat, lng + dlon, half,
                           sink, failures, truncated)
    return new


ADVISOR_COLUMNS = ["advisor_id", "external_id", "name", "first_name", "last_name",
                   "title", "email", "phone", "team_name", "office_name",
                   "office_manager", "market", "address1", "address2", "city",
                   "state", "postal", "lat", "lon", "website_url", "is_active"]


def normalise(rec: dict) -> dict:
    """API record -> our column names. `distanceMiles` is dropped: it is measured
    to whichever seed happened to find the advisor, so it means nothing once the
    circles are merged."""
    return {
        "advisor_id": rec.get("id", ""),
        "external_id": rec.get("externalId", ""),
        "name": (rec.get("displayName") or rec.get("name") or "").strip(),
        "first_name": rec.get("firstName") or "",
        "last_name": rec.get("lastName") or "",
        "title": rec.get("title") or "",
        "email": (rec.get("emailAddress") or "").strip(),
        "phone": rec.get("phone") or "",
        "team_name": rec.get("teamName") or "",
        "office_name": rec.get("officeName") or "",
        "office_manager": rec.get("officeManagerName") or "",
        "market": rec.get("marketID") or "",
        "address1": rec.get("address1") or "",
        "address2": rec.get("address2") or "",
        "city": rec.get("city") or "",
        "state": rec.get("state") or "",
        "postal": rec.get("postalCode") or "",
        "lat": rec.get("latitude") or "",
        "lon": rec.get("longitude") or "",
        "website_url": rec.get("websiteUrl") or "",
        "is_active": rec.get("isActive", ""),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mesh", type=float, default=MESH_MILES)
    ap.add_argument("--radius", type=float, default=RADIUS_MILES)
    ap.add_argument("--no-sweep", action="store_true", help="skip the national safety net")
    ap.add_argument("--dry-run", action="store_true", help="plan the seeds, query nothing")
    args = ap.parse_args()

    seeds = seed_points(args.mesh)
    if args.dry_run:
        total = len(seeds) + (0 if args.no_sweep else len(SWEEP))
        print(f"[*] dry run: {len(seeds)} mesh seeds at {args.radius:.0f} miles"
              f" + {0 if args.no_sweep else len(SWEEP)} sweep points at {MAX_RADIUS}"
              f" = {total} requests before any splitting")
        return

    started = time.time()
    session = requests.Session()
    sink: dict = {}
    failures: list = []
    truncated: list = []

    for i, (lat, lng) in enumerate(seeds, 1):
        new = collect(session, lat, lng, args.radius, sink, failures, truncated)
        print(f"[{i}/{len(seeds)}] ({lat:.2f}, {lng:.2f})  {new:>4} new  "
              f"|  total {len(sink):,}")
        time.sleep(PAUSE)

    mesh_total = len(sink)
    if not args.no_sweep:
        print(f"\n[*] national sweep at {MAX_RADIUS} miles ({len(SWEEP)} points) "
              f"-- anything found here is something the mesh missed")
        for i, (lat, lng) in enumerate(SWEEP, 1):
            new = collect(session, lat, lng, MAX_RADIUS, sink, failures, truncated)
            print(f"[sweep {i}/{len(SWEEP)}] ({lat:.1f}, {lng:.1f})  {new:>4} new  "
                  f"|  total {len(sink):,}")
            time.sleep(PAUSE)

    advisors = [r for (kind, _), r in sink.items() if kind == "FinancialAdvisor"]
    offices = [r for (kind, _), r in sink.items() if kind == "OfficeLocation"]
    if not advisors:
        raise SystemExit("no advisors collected -- refusing to write an empty roster")

    raw = scratch_path("baird", "api", ext="json")
    raw.write_text(json.dumps(list(sink.values()), indent=1), encoding="utf-8")

    df = pd.DataFrame([normalise(r) for r in advisors], columns=ADVISOR_COLUMNS)
    out = roster_path("baird")
    df.to_csv(out, index=False)

    # Offices are a different kind of record -- one row per building, with the
    # branch manager -- so they get their own file rather than being mixed into
    # a roster whose unit is the advisor.
    office_out = scratch_path("baird", "offices", ext="csv")
    pd.DataFrame([normalise(r) for r in offices],
                 columns=ADVISOR_COLUMNS).to_csv(office_out, index=False)

    emails = int((df["email"] != "").sum())
    print(f"\n[*] {len(df):,} advisors + {len(offices):,} offices in "
          f"{time.time() - started:.0f}s")
    print(f"    roster  -> {out}")
    print(f"    offices -> {office_out}")
    print(f"    raw     -> {raw}")
    print(f"    {emails:,} have an email address ({emails / len(df):.1%}); "
          f"{int((df['phone'] != '').sum()):,} have a phone")
    # teamName is NOT the team. It equals officeName in 100% of rows -- Baird
    # populates it with the branch, so "150 teams" would just be "150 offices"
    # wearing a different label. The real practice grouping is the shared
    # website: booselringwalagroup.bairdwealth.com covers Boosel, Ringwala and
    # Oser, who sit in one office with eleven other advisors.
    practices = df.loc[df["website_url"] != "", "website_url"]
    multi = practices.value_counts()
    print(f"    {df['state'].nunique()} states, {len(offices):,} offices, "
          f"{practices.nunique():,} practice websites "
          f"({int((multi > 1).sum()):,} shared by 2+ advisors = real teams)")

    if not args.no_sweep:
        print(f"    mesh found {mesh_total:,}; the national sweep added "
              f"{len(sink) - mesh_total:,} more -- if that is not near zero, "
              f"widen --mesh or lower --radius")

    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == BAIRD_CRD]["advisor_crd"].nunique()
        # Baird's API carries no CRD, so this is a COUNT comparison and nothing
        # more. It cannot tell us which advisors matched -- only the matcher can.
        #
        # Do not read the shortfall as missed coverage. The public directory
        # lists CLIENT-FACING advisors; ADV counts every investment adviser
        # representative, including institutional and support staff who are
        # never published. Evidence the crawl is complete: the national sweep at
        # 500 miles added zero, and the 11 states in ADV with no scraped advisor
        # hold 1-3 IARs each (~19 people total).
        print(f"    SEC lists {sec:,} IARs at CRD {BAIRD_CRD}; the public "
              f"directory publishes {len(df):,} client-facing advisors "
              f"({len(df) / sec:.0%}); identities unverified -- no CRD in this feed")
    if truncated:
        print(f"    {len(truncated)} circle(s) STILL FULL at the minimum radius "
              f"-- these are under-collected: {truncated}")
    if failures:
        print(f"    {len(failures)} circle(s) FAILED after {RETRIES} attempts: {failures}")


if __name__ == "__main__":
    main()
