"""Truist Wealth advisor roster -> data/raw/firm_rosters/truist_<date>.csv

    GET https://www.truist.com/truist-api/advisor-search-v2.json
        ?latlong=<lat,lng>&category=zip&q=<anything>&filters=profileType:wealth profile;

Like Baird, every profile carries a work EMAIL and a direct phone. Truist adds
two things no other source gives us: a structured `specialties` list (investment
management, estate planning, philanthropy...) and a free-text biography. It does
NOT give latitude/longitude -- only a street address and ZIP -- so placement has
to go through the ZIP centroid, and there is no CRD, so matching is by name and
geography.

MEASURED BEHAVIOUR (probed, not assumed)
----------------------------------------
    latlong      drives the search. `category` and `q` must be PRESENT but their
                 values are ignored -- passing q=99999 against Atlanta's latlong
                 returns Atlanta's 71 advisors unchanged. So no ZIP lookup is
                 needed; the pair is sent only to satisfy the endpoint.
    filters      counter-intuitively INCREASES the result set. Memphis returns
                 53 rows unfiltered and 73 with `profileType:wealth profile;`.
                 Always send it.
    limit        caps downward only. limit=50 returns 50; limit=250 or 500
                 returns whatever the page holds, around 70-80.
    totalCount   is the LENGTH OF THE PAGE, not the size of the result set.
                 offset=50 reports "totalCount": 80. Never treat it as a total.
    offset       genuinely pages, ordered by distance and walking outward.
                 Atlanta: offset 0 reaches 10.7 miles, offset 50 reaches 29.8
                 (38 of its 80 rows overlap the first page), offset 100 reaches
                 151. Overlap between pages is expected -- dedupe, don't assume.

Because one page covers only ~10 miles in a dense metro, the mesh here is
tighter than Cetera's or Baird's and each seed is paged outward until it has
covered its own cell. Seeds come from Truist's branch cities for CRD 283390.

Run:  python src/truist_async.py [--mesh 40] [--dry-run]
"""
from __future__ import annotations

import argparse
import html
import json
import math
import pathlib
import random
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path, scratch_path  # one naming convention, defined once

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
GEO_INDEX = ROOT / "webapp" / "data" / "geo_index.json"
BRANCHES = ROOT / "data" / "output" / "advisor_branches.parquet"

API_URL = "https://www.truist.com/truist-api/advisor-search-v2.json"
TRUIST_CRD = "283390"
FILTERS = "profileType:wealth profile;"
HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "accept-language": "en-US,en;q=0.9",
    "x-requested-with": "XMLHttpRequest",
    "referer": "https://www.truist.com/finder/wealth",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
}

MESH_MILES = 40      # tighter than elsewhere: one page covers ~10 miles downtown
COVER_MILES = 45     # page a seed outward until it has covered its own cell
PAGE_STEP = 50       # offset increment
MAX_PAGES = 12       # a seed that still has not covered its cell is reported
PAUSE = 0.6
RETRIES = 3


def miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d = (math.sin(p1) * math.sin(p2)
         + math.cos(p1) * math.cos(p2) * math.cos(math.radians(lon1 - lon2)))
    return 3959 * math.acos(max(-1.0, min(1.0, d)))


def seed_points(mesh: float) -> list[tuple[float, float]]:
    """Truist's own branch cities, thinned so no two seeds are within `mesh`."""
    if not GEO_INDEX.exists() or not BRANCHES.exists():
        raise SystemExit(f"need {GEO_INDEX} and {BRANCHES}; run the ADV pipeline first")

    geo = json.loads(GEO_INDEX.read_text(encoding="utf-8"))
    zips, cities = geo["zips"], geo["cities"]
    branches = pd.read_parquet(
        BRANCHES, columns=["firm_crd", "branch_city", "branch_state", "branch_postal"])
    branches = branches[branches["firm_crd"].astype(str) == TRUIST_CRD]

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
    print(f"[*] {len(branches):,} Truist branch rows -> {len(located):,} distinct places "
          f"-> {len(seeds)} query seeds at a {mesh:.0f}-mile mesh "
          f"({unresolved} rows had no centroid)")
    return seeds


def fetch(session: requests.Session, lat: float, lng: float, offset: int):
    """One page. Returns (results, ok); ok is False only if every attempt errored."""
    params = {"latlong": f"{lat:.6f},{lng:.6f}", "category": "zip", "q": "00000",
              "filters": FILTERS}
    if offset:
        params["offset"] = offset
    for attempt in range(RETRIES):
        try:
            r = session.get(API_URL, headers=HEADERS, params=params, timeout=90)
            if r.status_code == 200:
                return (r.json().get("results") or []), True
            print(f"    [-] HTTP {r.status_code} at ({lat}, {lng}) offset {offset}")
        except Exception as exc:
            print(f"    [-] {type(exc).__name__} at ({lat}, {lng}): {exc}")
        time.sleep(PAUSE * (2 ** attempt) + random.random())
    return [], False


def collect(session, lat, lng, sink, failures, short):
    """Page a seed outward until it has covered its own mesh cell.

    Stop when the furthest row on a page is past COVER_MILES (the neighbouring
    seed owns that ground), or when a page adds nothing new. Pages overlap by
    design, so 'no new records' is a real terminator, not an anomaly."""
    new_total = 0
    for page in range(MAX_PAGES):
        rows, ok = fetch(session, lat, lng, page * PAGE_STEP)
        if not ok:
            failures.append((lat, lng, page * PAGE_STEP))
            return new_total
        new = 0
        for rec in rows:
            key = rec.get("cqPagePath") or rec.get("dataNodePath")
            if key and key not in sink:
                sink[key] = rec
                new += 1
        new_total += new
        reach = max((float(r.get("distance") or 0) for r in rows), default=0)
        if not rows or reach >= COVER_MILES or (new == 0 and page > 0):
            return new_total
        time.sleep(PAUSE)
    short.append((lat, lng))          # never reached COVER_MILES -- under-covered
    return new_total


TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def plain(raw: str) -> str:
    """Profile bios arrive as CMS HTML. Strip it to text -- the markup is noise,
    and a stray </div> inside a CSV cell helps nobody."""
    return WS.sub(" ", html.unescape(TAGS.sub(" ", raw or ""))).strip()


COLUMNS = ["name", "first_name", "middle_initial", "last_name", "suffix", "title",
           "officer_title", "designations", "email", "phone", "address1",
           "address2", "city", "state", "postal", "specialties", "legacy_org",
           "node_type", "team_title", "profile_url", "bio"]


def normalise(rec: dict) -> dict:
    """API record -> our column names. `distance` is dropped: it is measured to
    whichever seed found the profile, so it means nothing once pages merge."""
    specialties = [s.get("title", "") for s in (rec.get("specialties") or [])
                   if s.get("title")]
    path = rec.get("cqPagePath") or ""
    return {
        "name": (rec.get("fullName") or rec.get("teamTitle") or "").strip(),
        "first_name": rec.get("firstName") or "",
        "middle_initial": rec.get("middleInitial") or "",
        "last_name": rec.get("lastName") or "",
        "suffix": rec.get("suffix") or "",
        "title": rec.get("title") or "",
        "officer_title": rec.get("wealthOfficerTitle") or "",
        "designations": rec.get("designationCode") or "",
        "email": (rec.get("emailAddress") or "").strip(),
        "phone": rec.get("phone") or "",
        "address1": rec.get("addressLine1") or "",
        "address2": rec.get("addressLine2") or "",
        "city": rec.get("city") or "",
        "state": rec.get("state") or "",
        "postal": rec.get("zip5") or "",
        "specialties": "; ".join(specialties),
        # Truist/hST/hBBT -- which predecessor bank the profile came from
        "legacy_org": rec.get("organization") or "",
        "node_type": rec.get("nodeType") or "",
        "team_title": rec.get("teamTitle") or "",
        "profile_url": f"https://www.truist.com{path.replace('/content/truist-bank/us/en', '')}"
                       if path else "",
        "bio": plain(rec.get("backgroundText")),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mesh", type=float, default=MESH_MILES)
    ap.add_argument("--dry-run", action="store_true", help="plan the seeds, query nothing")
    args = ap.parse_args()

    seeds = seed_points(args.mesh)
    if args.dry_run:
        print(f"[*] dry run: {len(seeds)} seeds, up to {MAX_PAGES} pages each")
        return

    started = time.time()
    session = requests.Session()
    sink: dict = {}
    failures: list = []
    short: list = []

    for i, (lat, lng) in enumerate(seeds, 1):
        new = collect(session, lat, lng, sink, failures, short)
        print(f"[{i}/{len(seeds)}] ({lat:.2f}, {lng:.2f})  {new:>4} new  "
              f"|  total {len(sink):,}")
        time.sleep(PAUSE)

    people = [r for r in sink.values() if r.get("nodeType") != "team"]
    teams = [r for r in sink.values() if r.get("nodeType") == "team"]
    if not people:
        raise SystemExit("no advisors collected -- refusing to write an empty roster")

    raw = scratch_path("truist", "api", ext="json")
    raw.write_text(json.dumps(list(sink.values()), indent=1), encoding="utf-8")

    df = pd.DataFrame([normalise(r) for r in people], columns=COLUMNS)
    out = roster_path("truist")
    df.to_csv(out, index=False)

    team_out = scratch_path("truist", "teams", ext="csv")
    pd.DataFrame([normalise(r) for r in teams], columns=COLUMNS).to_csv(
        team_out, index=False)

    emails = int((df["email"] != "").sum())
    print(f"\n[*] {len(df):,} advisors + {len(teams):,} team profiles in "
          f"{time.time() - started:.0f}s")
    print(f"    roster -> {out}")
    print(f"    teams  -> {team_out}")
    print(f"    raw    -> {raw}")
    print(f"    {emails:,} have an email ({emails / len(df):.1%}); "
          f"{int((df['phone'] != '').sum()):,} have a phone; "
          f"{int((df['specialties'] != '').sum()):,} have specialties")
    print(f"    {df['state'].nunique()} states, "
          f"{df['title'].nunique()} distinct titles")

    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == TRUIST_CRD]["advisor_crd"].nunique()
        # A COUNT comparison only -- no CRD in this feed, so it cannot say which
        # advisors matched. Do not read the shortfall as missed coverage: the
        # finder is client-facing, while ADV counts every IAR including staff
        # who are never published. Evidence the crawl is complete: of the 21
        # states where Truist files a branch, 20 are represented, and the one
        # that is not (IN) holds a single IAR.
        print(f"    SEC lists {sec:,} IARs at CRD {TRUIST_CRD}; the finder "
              f"publishes {len(df):,} client-facing advisors ({len(df) / sec:.0%})")
    if short:
        print(f"    {len(short)} seed(s) hit the {MAX_PAGES}-page cap without "
              f"covering {COVER_MILES} miles -- possibly under-collected: {short[:5]}")
    if failures:
        print(f"    {len(failures)} page(s) FAILED after {RETRIES} attempts: {failures[:5]}")


if __name__ == "__main__":
    main()
