"""Northwestern Mutual advisor roster -> data/raw/firm_rosters/northwestern_mutual_<date>.csv

    GET https://www.northwesternmutual.com/nmx-api-proxy/nmx/ms-search/fr-search-v2
        ?lat=<lat>&lng=<lng>&distance=30&addressTypes=&fullName=
        &page[number]=<n>&page[size]=25

Payload is excellent: work EMAIL, direct phone, full street address WITH
lat/lon, designations, areas of expertise, personal LinkedIn, practice website,
years of service, and a stable agentNumber. None of the tracing headers in the
browser request (x-dtpc, traceparent, x-pre-client-id) are required.

MEASURED BEHAVIOUR (probed, not assumed)
----------------------------------------
    distance    IGNORED. 5, 30 and 100 miles all return the SAME 138 advisors
                around Atlanta, reaching the same 20.0 miles. The endpoint has
                a fixed internal radius of roughly 20 miles and the parameter
                does nothing. This is the single most important fact here: a
                crawl designed around big radii would collect a fraction of the
                firm and look successful.
    page[size]  25 is the ceiling; 30 returns HTTP 500
    page[number] zero-based and real. There is NO total count anywhere in the
                response, so the only way to know a point is exhausted is to
                page until one comes back empty.
    sparse areas return an empty list rather than widening -- rural Kansas and
                rural Montana both give 0 at every distance.

Because the reach is ~20 miles and fixed, the crawl is a NATIONAL 25-mile mesh
over ZIP centroids (1,662 seeds), not the firm's own branch cities. NM's ADV
registration covers 2,130 IARs but the public directory lists financial
representatives, a much larger and more widely spread population, so seeding
from ADV branches alone would miss the places where reps but no IAR sit.

A WORD ON WHETHER THIS IS A PROSPECT LIST
-----------------------------------------
Both NM entities score is_target=False. Its representatives are captive and
insurance-led, and the in-house manager (CRD 307865, $323B) is a competitor
rather than a buyer. Treat this roster as reach data -- useful for the map and
for knowing who is in a territory -- not as a qualified target list.

Run:  python src/northwestern_mutual_async.py [--mesh 25] [--dry-run]
"""
from __future__ import annotations

import argparse
import concurrent.futures
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

API = ("https://www.northwesternmutual.com/nmx-api-proxy/nmx/ms-search/"
       "fr-search-v2")
NM_CRD = "2881"
HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "referer": "https://www.northwesternmutual.com/financial-professionals/",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
}

MESH_MILES = 25      # the endpoint reaches ~20, so seeds must be closer than that
PAGE_SIZE = 25       # server ceiling; 30 is a 500
MAX_PAGES = 60       # Manhattan needs 13; 60 is a runaway guard, not a limit
PAUSE = 0.3
RETRIES = 3


def miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d = (math.sin(p1) * math.sin(p2)
         + math.cos(p1) * math.cos(p2) * math.cos(math.radians(lon1 - lon2)))
    return 3959 * math.acos(max(-1.0, min(1.0, d)))


def seed_points(mesh: float) -> list:
    """A national mesh over ZIP centroids, densest first.

    Not the firm's branch cities: NM publishes financial representatives, who
    are far more numerous and more widely spread than the 2,130 IARs on its
    ADV, so an ADV-derived mesh would have holes exactly where the extra reps
    are."""
    if not GEO_INDEX.exists():
        raise SystemExit(f"need {GEO_INDEX}; run the ADV pipeline first")
    zips = json.loads(GEO_INDEX.read_text(encoding="utf-8"))["zips"]
    # densest ZIPs first so the retained seeds sit in populated places
    ordered = sorted(zips.values(), key=lambda v: -(v[3] or 0))
    seeds: list = []
    for entry in ordered:
        lat, lon = entry[1], entry[2]
        if all(miles(lat, lon, s[0], s[1]) > mesh for s in seeds):
            seeds.append((lat, lon))
    print(f"[*] {len(zips):,} ZIP centroids -> {len(seeds):,} seeds at a "
          f"{mesh:.0f}-mile mesh")
    return seeds


def fetch(session: requests.Session, lat: float, lng: float, page: int):
    params = {"address": "", "lat": lat, "lng": lng, "addressTypes": "",
              # sent because the browser sends it; it is documented above as
              # having no effect, and is kept only to match the real client
              "distance": 30, "fullName": "",
              "page[number]": page, "page[size]": PAGE_SIZE}
    for attempt in range(RETRIES):
        try:
            r = session.get(API, headers=HEADERS, params=params, timeout=90)
            if r.status_code == 200:
                payload = r.json()
                return (payload if isinstance(payload, list) else []), True
            print(f"    [-] HTTP {r.status_code} at ({lat:.2f}, {lng:.2f}) p{page}")
        except Exception as exc:
            print(f"    [-] {type(exc).__name__} at ({lat:.2f}, {lng:.2f}): {exc}")
        time.sleep(PAUSE * (2 ** attempt) + random.random())
    return [], False


def collect(session, lat, lng, sink, failures, capped):
    """Page one point to exhaustion.

    There is no result count in the response, so 'exhausted' can only mean 'a
    page came back empty'. Stopping on a short page would be wrong -- the API
    returns full pages until it runs out."""
    new = 0
    for page in range(MAX_PAGES):
        rows, ok = fetch(session, lat, lng, page)
        if not ok:
            failures.append((round(lat, 3), round(lng, 3), page))
            return new
        if not rows:
            return new
        for rec in rows:
            key = rec.get("agentNumber") or rec.get("id")
            if key and key not in sink:
                sink[key] = rec
                new += 1
        time.sleep(PAUSE)
    capped.append((round(lat, 3), round(lng, 3)))
    return new


COLUMNS = ["name", "first_name", "last_name", "title", "designations", "email",
           "phone", "phone_label", "address1", "building", "city", "state",
           "postal", "lat", "lon", "areas_of_expertise", "languages",
           "years_of_service", "is_pcg", "website", "linkedin", "profile_slug",
           "agent_number"]
# Appended after the legacy header so a consumer reading by name is unaffected.
ADDED = ["leid", "team_site", "team_page", "is_core", "is_active", "source"]


def normalise(rec: dict) -> dict:
    phone = rec.get("selectedPhone1") or {}
    social = rec.get("socialMedia") or {}
    slug = rec.get("slug") or ""
    return {
        "name": (rec.get("fullName") or "").strip(),
        "first_name": (rec.get("firstName") or "").strip(),
        "last_name": (rec.get("lastName") or "").strip(),
        "title": rec.get("title") or "",
        "designations": ", ".join(rec.get("designations") or []),
        "email": (rec.get("email") or "").strip(),
        "phone": phone.get("Number") or "",
        "phone_label": phone.get("Label") or "",
        "address1": rec.get("street") or "",
        "building": rec.get("building") or "",
        "city": rec.get("city") or "",
        "state": rec.get("state") or "",
        "postal": rec.get("zip") or "",
        "lat": rec.get("latitude") or "",
        "lon": rec.get("longitude") or "",
        "areas_of_expertise": "; ".join(rec.get("areasOfExpertise") or []),
        "languages": "; ".join(rec.get("languages") or []),
        "years_of_service": rec.get("lengthOfService") or "",
        # Private Client Group -- NM's own high-net-worth tier
        "is_pcg": rec.get("isPcg", ""),
        "website": rec.get("websiteUrl") or "",
        "linkedin": social.get("linkedinUrl") or "",
        "profile_slug": slug,
        "agent_number": rec.get("agentNumber") or "",
    }


# --------------------------------------------------------------------------
# Phase 2 -- practice websites
# --------------------------------------------------------------------------
# Every Northwestern Mutual practice site, on an *.nm.com subdomain or a vanity
# domain, is the same CMS. The page ships NOTHING in its markup -- there is not
# a single <a href> on it -- and renders from three JSON blobs declared in a
# plain <script>: `siteData`, `entityData` and `site`. `site` holds both the
# sitemap and the team member records, so one fetch of the root gives the list
# of pages to visit next.
DECODER = json.JSONDecoder()
SITE_HEADERS = {"user-agent": HEADERS["user-agent"],
                "accept": "text/html,application/xhtml+xml"}
# What these sites call the page their people are on. Discovered from the
# sitemap rather than guessed, because the naming is not consistent: my-team,
# our-team, meet-our-team, who-we-are/team, about-us/meet-the-team.
TEAM_PAGE = re.compile(r"(^|/)(my|our|the)?-?team|meet-.*-team|our-people|who-we-are",
                       re.I)


def blob(html: str, name: str):
    """One `const <name> = {...};` out of the page.

    Brace-counted extraction does not work here: the objects embed HTML and
    JavaScript in their string values -- the cookie banner alone carries a
    <script> tag -- so a naive scan closes the object hundreds of characters
    early. raw_decode understands strings and stops in the right place.
    """
    match = re.search(r"const\s+%s\s*=\s*" % name, html)
    if not match:
        return None
    try:
        start = html.index("{", match.end() - 1)
        return DECODER.raw_decode(html, start)[0]
    except (ValueError, IndexError):
        return None


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def field(value):
    """The CMS wraps every value as {"PartialID": ..., "Value": ...}."""
    return value.get("Value") if isinstance(value, dict) else value


def members(html: str) -> list:
    """The people named on one practice page."""
    site = blob(html, "site")
    if not site:
        return []
    out = []
    for node in walk(site):
        if not (isinstance(node, dict) and "FirstName" in node and "Email" in node):
            continue
        email = str(field(node.get("Email")) or "").strip().lower()
        first = str(field(node.get("FirstName")) or "").strip()
        last = str(field(node.get("LastName")) or "").strip()
        if not email.endswith("@nm.com") or not (first or last):
            continue
        designations = field(node.get("Designations")) or ""
        if isinstance(designations, str) and designations.startswith("["):
            try:
                designations = ", ".join(json.loads(designations))
            except ValueError:
                designations = ""
        out.append({
            "name": " ".join(f"{first} {last}".split()),
            "first_name": first, "last_name": last,
            "title": str(field(node.get("Title")) or "").strip(),
            "designations": designations if isinstance(designations, str) else "",
            "email": email,
            "linkedin": str(field(node.get("LinkedIn")) or "").strip(),
            "leid": str(field(node.get("LEID")) or "").strip(),
            # NM's own flags, kept rather than interpreted. is_core is the
            # practice's own answer to "is this person an advisor or support".
            "is_core": field(node.get("IsCore")),
            "is_active": field(node.get("IsActive")),
        })
    return out


def team_pages(html: str) -> list:
    """The sitemap's own team pages, as relative paths."""
    site = blob(html, "site")
    if not site:
        return []
    out = []
    for node in walk(site):
        if not (isinstance(node, dict) and "FriendlyURL" in node):
            continue
        url = str(node.get("FriendlyURL") or "").strip()
        name = str(node.get("Name") or "")
        if not url or url.startswith("http"):
            continue
        if TEAM_PAGE.search(url) or TEAM_PAGE.search(name):
            out.append(url)
    return list(dict.fromkeys(out))


def fetch_site(session, url, tries=3):
    for attempt in range(tries):
        try:
            r = session.get(url, headers=SITE_HEADERS, timeout=45,
                            allow_redirects=True)
            if r.status_code == 200:
                return r.text
            if r.status_code in (404, 410):
                return ""
        except Exception:
            pass
        time.sleep(1.5 * (2 ** attempt) + random.random())
    return ""


def one_site(host, pause):
    """Root page, then whatever team pages the sitemap names."""
    session = requests.Session()
    root = fetch_site(session, "https://" + host)
    if not root:
        return host, {}
    people = {m["email"]: (m, "") for m in members(root)}
    for path in team_pages(root):
        page = fetch_site(session, f"https://{host}/{path.lstrip('/')}")
        if page:
            for m in members(page):
                people.setdefault(m["email"], (m, path))
        time.sleep(pause)
    return host, people


def sweep_sites(hosts, pause, workers=8):
    """Every practice site, a few at a time.

    Concurrency is across DIFFERENT hosts, never within one: each site sees at
    most one request at a time and two or three in total. These are thousands
    of small third-party servers, several on advisors' own domains, and they
    did not ask to be crawled.
    """
    found, done, reached = {}, 0, 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one_site, host, pause) for host in hosts]
        for future in concurrent.futures.as_completed(futures):
            try:
                host, people = future.result()
            except Exception as exc:
                print(f"    [-] {type(exc).__name__} on a site")
                continue
            done += 1
            if people:
                reached += 1
                found[host] = people
            if done % 250 == 0 or done == len(hosts):
                print(f"    {done:>5}/{len(hosts)} sites  {reached:,} yielded people  "
                      f"{sum(len(v) for v in found.values()):,} found")
    return found


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mesh", type=float, default=MESH_MILES)
    ap.add_argument("--limit", type=int, help="only the first N seeds, for a trial")
    ap.add_argument("--dry-run", action="store_true", help="plan the mesh, query nothing")
    ap.add_argument("--skip-sites", action="store_true",
                    help="directory only, skip the practice websites")
    ap.add_argument("--site-pause", type=float, default=0.25,
                    help="seconds between website requests; these are thousands "
                         "of small third-party hosts, so be gentle")
    args = ap.parse_args()

    seeds = seed_points(args.mesh)
    if args.limit:
        seeds = seeds[:args.limit]
    if args.dry_run:
        print(f"[*] dry run: would query {len(seeds):,} seeds, paging each to "
              f"exhaustion at {PAGE_SIZE}/page")
        return

    started = time.time()
    session = requests.Session()
    sink: dict = {}
    failures, capped = [], []

    for i, (lat, lng) in enumerate(seeds, 1):
        new = collect(session, lat, lng, sink, failures, capped)
        if i % 100 == 0 or new > 40:
            print(f"[{i}/{len(seeds)}] ({lat:.2f}, {lng:.2f})  {new:>4} new  "
                  f"|  total {len(sink):,}")
        time.sleep(PAUSE)

    if not sink:
        raise SystemExit("nothing collected -- refusing to write an empty roster")

    records = list(sink.values())
    scratch_path("northwestern_mutual", "api", ext="json").write_text(
        json.dumps(records, indent=1), encoding="utf-8")

    rows = [normalise(r) for r in records]
    for row in rows:
        row.update({"leid": "", "team_site": row["website"], "team_page": "",
                    "is_core": "", "is_active": "", "source": "directory"})

    if not args.skip_sites:
        hosts = sorted({r["website"].strip().lower() for r in rows if r.get("website")})
        print(f"[*] phase 2: {len(hosts):,} practice websites")
        pages = sweep_sites(hosts, args.site_pause)

        # Email is the join. The directory has no LEID and the sites have no
        # agent number, so the address -- always @nm.com on both sides -- is the
        # only key the two share.
        known = {r["email"].strip().lower(): r for r in rows if r.get("email")}
        added = filled = 0
        for host, people in pages.items():
            for email, (member, path) in people.items():
                existing = known.get(email)
                if existing is not None:
                    # Already in the directory: keep the directory's version and
                    # take only what it did not have.
                    existing["leid"] = existing["leid"] or member["leid"]
                    existing["team_page"] = existing["team_page"] or path
                    for key in ("title", "designations", "linkedin"):
                        if member.get(key) and not existing.get(key):
                            existing[key] = member[key]
                            filled += 1
                    continue
                row = {column: "" for column in COLUMNS}
                row.update(member)
                row["website"] = host
                row["team_site"] = host
                row["team_page"] = path
                row["source"] = "team_page"
                rows.append(row)
                known[email] = row
                added += 1
        print(f"[*] practice websites added {added:,} people the directory does "
              f"not list, and filled {filled:,} blank fields on people it does")

        # A team page names its people but not where they sit, so every person
        # it added arrived with no city, state or coordinates. That is not a
        # cosmetic gap: without an address the matcher has only a name to work
        # with, and Northwestern Mutual's review tier went from 3,265 to 6,325
        # the first time these rows shipped without one.
        #
        # The practice's own site IS the address. Everyone on it works out of
        # the office the directory already records for whoever owns that site,
        # so the location is inherited from them rather than left empty.
        WHERE = ("address1", "building", "city", "state", "postal", "lat", "lon")
        home = {}
        for row in rows:
            if row["source"] == "directory" and row.get("city"):
                home.setdefault(row["website"].strip().lower(),
                                {k: row.get(k, "") for k in WHERE})
        placed = 0
        for row in rows:
            if row["source"] == "directory" or row.get("city"):
                continue
            at = home.get(row["website"].strip().lower())
            if not at:
                continue
            for key, value in at.items():
                row[key] = row.get(key) or value
            placed += 1
        print(f"[*] {placed:,} of them inherited the office address from the "
              f"advisor whose site it is")

    df = pd.DataFrame(rows, columns=COLUMNS + ADDED).fillna("")
    out = roster_path("northwestern_mutual")
    df.to_csv(out, index=False)

    emails = int((df["email"] != "").sum())
    print(f"\n[*] {len(df):,} advisors in {time.time() - started:.0f}s -> {out}")
    print(f"    {emails:,} have an email ({emails / len(df):.1%}); "
          f"{int((df['phone'] != '').sum()):,} a phone; "
          f"{int((df['lat'] != '').sum()):,} have coordinates")
    print(f"    {df['state'].replace('', pd.NA).nunique()} states; "
          f"{int((df['linkedin'] != '').sum()):,} a personal LinkedIn; "
          f"{int((df['website'] != '').sum()):,} a practice website")
    pcg = int((df["is_pcg"] == True).sum()) if "is_pcg" in df else 0
    print(f"    {pcg:,} flagged Private Client Group (NM's high-net-worth tier)")

    if "source" in df:
        print("    by source: " + ", ".join(
            f"{k} {v:,}" for k, v in df["source"].value_counts().items()))
        core = df[df["source"] == "team_page"]["is_core"].astype(str).str.lower()
        print(f"    of the website rows, {int((core == 'true').sum()):,} are flagged "
              f"IsCore by the practice and {int((core != 'true').sum()):,} are not "
              f"-- NM's own answer to advisor vs support, kept rather than applied")

    directory = df[df["source"] == "directory"] if "source" in df else df
    dupes = len(directory) - directory["agent_number"].nunique()
    if dupes:
        print(f"    [!] {dupes} duplicate agent number(s) -- the key should be unique")

    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == NM_CRD]["advisor_crd"].nunique()
        # Expect WELL over 100%: the directory lists financial representatives,
        # while ADV counts only investment adviser representatives.
        print(f"    SEC lists {sec:,} IARs at CRD {NM_CRD}; the directory lists "
              f"{len(df):,} financial representatives ({len(df) / sec:.0%}) -- "
              f"most NM reps are insurance-led and never appear on an ADV")
    if capped:
        print(f"    {len(capped)} seed(s) hit the {MAX_PAGES}-page guard: {capped[:5]}")
    if failures:
        print(f"    {len(failures)} request(s) FAILED after {RETRIES} attempts: "
              f"{failures[:5]}")


if __name__ == "__main__":
    main()
