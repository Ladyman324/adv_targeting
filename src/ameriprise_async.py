"""Ameriprise advisor roster -> data/raw/firm_rosters/ameriprise_<date>.csv

    POST https://www.ameripriseadvisors.com/api/locatorsearch/search/
         criteria={"searchTerm":"atlanta, GA","numberOfRowsToReturn":250,
                   "startRowIndex":0,"radialDistance":0,"searchType":"city, state", ...}

The largest roster we scrape and the richest: name, designations, title, work
EMAIL, direct phone, full street address WITH lat/lon, team name, profile URL,
an internal advisor GUID, and a client-satisfaction score.

WHY CITY PAGES RATHER THAN A GEOGRAPHIC MESH
--------------------------------------------
Ameriprise publishes its own city index -- 51 state pages listing 2,071 cities
between them -- so the authoritative list of places it has advisors is simply
given to us. A lat/lon mesh would be guesswork by comparison, and `searchType`
only accepts "city, state": zip and state both return HTTP 400.

MEASURED LIMITS (probed, not assumed)
-------------------------------------
    numberOfRowsToReturn  250 is the ceiling; 300 returns HTTP 404
    startRowIndex         real paging. Atlanta: 83 total, offset 0 -> 50 rows,
                          offset 50 -> 33 rows
    resultCount           the TRUE total for the query, not the page length --
                          unlike Truist's totalCount. Page until it is reached.
    radialDistance        0 keeps the query to the named city. Deliberate: at 25
                          Atlanta jumps 83 -> 155 and starts returning the
                          neighbouring cities' advisors, which we visit anyway.
    nextRadiusTier        the site's "search wider" affordance. Ignored, same
                          reason.

TWO TRAPS IN THE PAYLOAD
------------------------
1. A result's location is NOT the city searched. Atlanta returns Reveal Wealth
   Strategies at 706 miles because that team has an office elsewhere that also
   matched. Every advisor is therefore written with their OWN primary location,
   never the search term, and deduped on the advisor GUID.
2. clientSatisfactionScore uses -1 as "no rating", not null -- and
   clientSatisfactionPercentage uses -20.0. Averaging the raw column would drag
   every practice below zero. Sentinels are converted to blank on the way out.

Run:  python src/ameriprise_async.py [--dry-run] [--states georgia,florida]
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
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
BRANCHES = ROOT / "data" / "output" / "advisor_branches.parquet"

INDEX = "https://www.ameripriseadvisors.com/find-a-financial-advisor-by-state/"
API = "https://www.ameripriseadvisors.com/api/locatorsearch/search/"
AMERIPRISE_CRD = "6363"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

PAGE_SIZE = 250      # server ceiling; 300 is a 404
MAX_PAGES = 40       # 10,000 advisors in one city would be a bug, not a city
PAUSE = 0.5
RETRIES = 3

STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district-of-columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new-hampshire": "NH", "new-jersey": "NJ", "new-mexico": "NM", "new-york": "NY",
    "north-carolina": "NC", "north-dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode-island": "RI",
    "south-carolina": "SC", "south-dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west-virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def text(raw: str) -> str:
    return WS.sub(" ", htmllib.unescape(TAGS.sub("", raw or ""))).strip()


def discover(session: requests.Session, only: set | None):
    """Ameriprise's own state -> city index. Returns [(city, STATE_CODE)]."""
    page = session.get(INDEX, headers={"user-agent": UA}, timeout=60)
    page.raise_for_status()
    states = sorted(set(re.findall(
        r'/find-a-financial-advisor-by-state/([a-z-]+)/"', page.text)))
    if only:
        states = [s for s in states if s in only]

    places, unknown = [], []
    for slug in states:
        code = STATE_CODES.get(slug)
        if not code:
            unknown.append(slug)
            continue
        try:
            body = session.get(f"{INDEX}{slug}/", headers={"user-agent": UA},
                               timeout=60).text
        except Exception as exc:
            print(f"    [-] {slug}: {type(exc).__name__} {exc}")
            continue
        cities = sorted(set(re.findall(
            rf"/find-a-financial-advisor-by-state/{slug}/([a-z0-9-]+)/", body)))
        places += [(c.replace("-", " "), code) for c in cities]
        time.sleep(PAUSE / 2)

    if unknown:
        # A state slug we cannot map is a silent hole in coverage, so say so
        # rather than skipping quietly.
        print(f"    [!] unmapped state slug(s), SKIPPED: {unknown}")
    print(f"[*] {len(states)} states -> {len(places):,} city pages")
    return places


def fetch(session: requests.Session, city: str, code: str, offset: int):
    criteria = {"searchTerm": f"{city}, {code}", "sortExpression": None,
                "numberOfRowsToReturn": PAGE_SIZE, "startRowIndex": offset,
                "radialDistance": 0, "defaultRadius": 0, "latitude": 0,
                "longitude": 0, "searchType": "city, state"}
    headers = {"user-agent": UA, "accept": "*/*", "x-requested-with": "XMLHttpRequest",
               "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
               "referer": INDEX}
    for attempt in range(RETRIES):
        try:
            r = session.post(API, headers=headers, timeout=120,
                             data={"criteria": json.dumps(criteria)})
            if r.status_code == 200:
                payload = r.json()
                return (payload.get("locatorResultModels") or [],
                        payload.get("resultCount") or 0, True)
            print(f"    [-] HTTP {r.status_code} for {city}, {code} @{offset}")
        except Exception as exc:
            print(f"    [-] {type(exc).__name__} for {city}, {code}: {exc}")
        time.sleep(PAUSE * (2 ** attempt) + random.random())
    return [], 0, False


def collect(session, city, code, sink, failures, short):
    """Page one city until resultCount is satisfied."""
    new = total = 0
    for page in range(MAX_PAGES):
        rows, total, ok = fetch(session, city, code, page * PAGE_SIZE)
        if not ok:
            failures.append(f"{city}, {code}")
            return new
        for rec in rows:
            # advisorId identifies a person, teamId a practice; the unused one
            # is the all-zero GUID, so the pair is a safe composite key.
            key = (rec.get("advisorId"), rec.get("teamId"))
            if key not in sink:
                sink[key] = rec
                new += 1
        if (page + 1) * PAGE_SIZE >= total or not rows:
            return new
        time.sleep(PAUSE)
    short.append(f"{city}, {code} ({total})")
    return new


def sentinel(value, bad=(-1, -1.0, -20.0)):
    """-1 and -20.0 mean 'no rating'. Returned blank so nobody averages them."""
    return "" if value is None or value in bad else value


COLUMNS = ["name", "title", "designations", "email", "phone", "address1",
           "address2", "city", "state", "postal", "lat", "lon", "team_name",
           "reports_to",
           "advisor_tier", "team_tier", "satisfaction_score", "satisfaction_reviews",
           "profile_url", "advisor_id", "team_id", "record_type", "n_locations"]


def normalise(rec: dict) -> dict:
    locations = rec.get("locations") or []
    # An advisor's own primary office -- NOT the city we searched, which may be
    # hundreds of miles away when a teammate's office is what matched.
    home = next((l for l in locations if l.get("isPrimary")),
                locations[0] if locations else {})
    designations = rec.get("designations") or []
    url = rec.get("advisorURL") or ""
    return {
        "name": (rec.get("displayName") or "").strip(),
        "title": rec.get("title") or "",
        "designations": ", ".join(text(d) for d in designations if text(d)),
        "email": (rec.get("email") or "").strip(),
        "phone": rec.get("phone") or "",
        "address1": home.get("address1") or "",
        "address2": home.get("address2") or "",
        "city": home.get("city") or "",
        "state": home.get("state") or "",
        "postal": home.get("postal") or "",
        "lat": home.get("lat") or "",
        "lon": home.get("lon") or "",
        # As Ameriprise gives it. Resolved to a practice name by
        # resolve_practices() below -- see there for why it needs resolving.
        "team_name": rec.get("teamName") or "",
        "reports_to": "",
        "advisor_tier": rec.get("advisorTier") or "",
        "team_tier": rec.get("teamTier") or "",
        "satisfaction_score": sentinel(rec.get("clientSatisfactionScore")),
        "satisfaction_reviews": sentinel(rec.get("clientSatisfactionReviews")),
        "profile_url": f"https://www.ameripriseadvisors.com{url}" if url else "",
        "advisor_id": rec.get("advisorId") or "",
        "team_id": rec.get("teamId") or "",
        "record_type": "team" if rec.get("isTeam") else "individual",
        "n_locations": len(locations),
    }


def resolve_practices(people, practices):
    """Turn `teamName` into the name of a PRACTICE.

    Ameriprise's teamName is a parent pointer, not always a team. For an
    advisor who leads their own practice it holds the practice -- John (JJ)
    Hughes reads "Kuttin Wealth Management". For the four advisors under him in
    Dothan it holds "John (JJ) Hughes", the person. So the field means "who I
    report to", and that is sometimes a practice and sometimes a colleague.

    Taken literally, 4,012 of the 7,104 advisors with a team were filed under a
    human being: William Colvin's practice was "Steven R. Knowles", Wesley
    Wells's was "Jonathan Kuttin".

    The chain is followed until it reaches a name that is not another advisor
    in this file. Measured against the finder's own 1,616 practice records, it
    is exactly ONE hop deep -- nobody needed two -- but the loop is written to
    take more and to give up on a cycle rather than assume that holds forever.

    The raw value is kept in `reports_to`. Who reports to whom is real
    information, and it is the only place this feed states it.

    Returns (resolved, adopted) -- see the second pass at the end for what a
    lead adopting their own name means.
    """
    parent, ambiguous = {}, {}
    for row in people:
        parent[(row["name"], row["state"])] = row["team_name"]
        ambiguous.setdefault(row["name"], set()).add(row["team_name"])

    def step(name, state):
        """That person's own teamName, or None if they are not in the file."""
        if (name, state) in parent:
            return parent[(name, state)]
        # Seven names are held by more than one advisor. Where the state does
        # not disambiguate and their teamNames disagree, the chain stops rather
        # than picking one.
        seen = ambiguous.get(name)
        return next(iter(seen)) if seen and len(seen) == 1 else None

    resolved = 0
    for row in people:
        raw = row["team_name"]
        if not raw:
            continue
        current, visited = raw, {row["name"]}
        for _ in range(6):
            if current in practices or current in visited:
                break
            nxt = step(current, row["state"])
            # Not an advisor in the file: it is already a practice name, even
            # if the finder does not publish a page for it.
            if nxt is None:
                break
            visited.add(current)
            # The chain ends at somebody with no parent of their own. The
            # practice really is theirs, and their name is the best label there
            # is -- so it stays, rather than being blanked.
            if not nxt:
                break
            current = nxt
        if current != raw:
            row["reports_to"] = raw
            row["team_name"] = current
            resolved += 1

    # A practice named after its lead does not contain its own lead.
    #
    # 378 advisors are named as the team of somebody else while their own
    # teamName is blank -- Steven R. Knowles leads a practice called "Steven R.
    # Knowles" and was filed as belonging to nothing. On the map that splits a
    # practice in two: his people group under his name and he sits outside it,
    # so opening any of them does not show him and opening him does not show
    # them. Giving him his own name closes the group.
    #
    # Matched within a STATE. Two advisors can share a name, and adopting a
    # team on a bare name match would put an unrelated namesake at the head of
    # somebody else's practice.
    leads = {(row["team_name"], row["state"]) for row in people if row["team_name"]}
    adopted = 0
    for row in people:
        if not row["team_name"] and (row["name"], row["state"]) in leads:
            row["team_name"] = row["name"]
            adopted += 1

    return resolved, adopted


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--states", help="comma-separated state slugs, for a partial run")
    ap.add_argument("--dry-run", action="store_true", help="list cities, query nothing")
    args = ap.parse_args()

    session = requests.Session()
    only = set(args.states.split(",")) if args.states else None
    places = discover(session, only)
    if args.dry_run:
        print(f"[*] dry run: would query {len(places):,} cities")
        return

    started = time.time()
    sink: dict = {}
    failures, short = [], []

    for i, (city, code) in enumerate(places, 1):
        new = collect(session, city, code, sink, failures, short)
        if i % 25 == 0 or new:
            print(f"[{i}/{len(places)}] {city}, {code}  {new:>4} new  "
                  f"|  total {len(sink):,}")
        time.sleep(PAUSE)

    if not sink:
        raise SystemExit("nothing collected -- refusing to write an empty roster")

    records = list(sink.values())
    scratch_path("ameriprise", "api", ext="json").write_text(
        json.dumps(records, indent=1), encoding="utf-8")

    rows = [normalise(r) for r in records]
    people = [r for r in rows if r["record_type"] == "individual"]
    teams = [r for r in rows if r["record_type"] == "team"]

    practice_names = {r["name"] for r in teams if r["name"]}
    promoted, adopted = resolve_practices(people, practice_names)

    df = pd.DataFrame(people, columns=COLUMNS)
    out = roster_path("ameriprise")
    df.to_csv(out, index=False)

    team_out = scratch_path("ameriprise", "teams", ext="csv")
    pd.DataFrame(teams, columns=COLUMNS).to_csv(team_out, index=False)

    emails = int((df["email"] != "").sum())
    coords = int((df["lat"] != "").sum())
    print(f"\n[*] {len(df):,} advisors + {len(teams):,} team profiles in "
          f"{time.time() - started:.0f}s")
    print(f"    roster -> {out}")
    print(f"    teams  -> {team_out}")
    print(f"    {emails:,} have an email ({emails / len(df):.1%}); "
          f"{int((df['phone'] != '').sum()):,} a phone; {coords:,} have coordinates")
    named = df["team_name"].replace("", pd.NA)
    in_list = int(df["team_name"].isin(practice_names).sum())
    print(f"    {df['state'].nunique()} states, "
          f"{int(named.notna().sum()):,} name a team across {named.nunique():,} practices, "
          f"{int((df['satisfaction_score'] != '').sum()):,} carry a rating")
    print(f"    {promoted:,} had a colleague's NAME where the practice belongs and "
          f"were resolved through it; {in_list:,} now match a published practice")
    print(f"    {adopted:,} practice leads had no team of their own and now carry "
          f"their own name, so a practice includes the person it is named after")

    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == AMERIPRISE_CRD]["advisor_crd"].nunique()
        # COUNT comparison only -- no CRD in this feed.
        print(f"    SEC lists {sec:,} IARs at CRD {AMERIPRISE_CRD}; the finder "
              f"publishes {len(df):,} ({len(df) / sec:.0%})")
    if short:
        print(f"    {len(short)} city(ies) hit the {MAX_PAGES}-page cap: {short[:5]}")
    if failures:
        print(f"    {len(failures)} city(ies) FAILED after {RETRIES} attempts: "
              f"{failures[:8]}")


if __name__ == "__main__":
    main()
