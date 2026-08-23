"""Merrill Lynch advisor roster -> data/raw/firm_rosters/merrill_<date>.csv

    GET https://liveapi.yext.com/v2/accounts/me/answers/vertical/query
        experienceKey=merrill_answers  verticalKey=financial_professionals
    GET https://advisor.ml.com/sites/<state>/<office>/<team>

WHAT CHANGED, AND WHY THE OLD SHAPE WAS WRONG
---------------------------------------------
The previous version read the Yext CONTENT stream `linkedAdvisorsStream`, which
is a stream of OFFICES. Each office document carries a `name` and a list of
`c_linkedFinancialProfessionals`, and that office name was written into the
roster's "Team Name" column. Which is why every advisor in Buckhead was on a
team called "Atl Pw" and 134 people shared one called "5av Fin Ctr" -- those are
Merrill office codes, not teams. Rod Westmoreland heads the Westmoreland Group;
the file said "Atl Pw".

The Answers vertical publishes the real thing, per person, in
`c_teamNameAndSite`:

    [{"teamName": "Westmoreland Group",
      "teamSite": "https://advisor.ml.com/sites/ga/atl-pw/westmorelandgroup",
      "teamEntityId": "27081671"}]

It also needs no Cloudflare clearance, no Chrome debug port and no cookies, so
the whole CDP apparatus -- native tab opening, polling a tab title for "Just a
moment", Playwright cookie extraction -- is gone with it.

OFFSET CAPS AT 10,000, SO THE SWEEP IS PER STATE
------------------------------------------------
The vertical holds ~10,966 professionals, `limit` is capped at 50 (error 9408
above that) and an `offset` of 10,000 or more is refused outright (error 9420).
A plain paging loop therefore stops 966 people short while looking like it ran
to completion. Partitioned on `address.region` instead: no state comes close to
the ceiling, and the total is checked against the directory's own count every
run so a silent shortfall cannot pass.

TEAM PAGES CARRY PEOPLE THE DIRECTORY DOES NOT
----------------------------------------------
David Streib is on the Westmoreland Group's page and returns NOTHING from the
Answers vertical -- not a weak match, zero results. Team pages embed their full
member list as percent-encoded JSON using the same field names the API uses, so
each distinct `teamSite` is fetched and its members merged in. Same two-step
shape as src/ubs_async.py, for the same reason: the searchable directory is a
subset of who is actually published.

THREE SOURCES, NOT ONE
----------------------
The office stream is still read, last, because 301 advisors are in it and NOT
in the searchable directory -- Edgar Manjarrez, Charlie Erdmann, Richard Mansour
and 298 others all return no match by name. Replacing the stream outright would
have swapped a wrong-team bug for a missing-people one. It contributes only
people the first two passes did not find, and its team names come from
`c_preferredLinkedTeam`, never from the enclosing document.

MEASURED 2026-08-20: 10,966 from the directory (all of them), 7,391 more from
the office stream, 510 more from 3,292 team pages, and 23,013 blank fields on
people already found filled in by those pages. 18,867 rows against 11,216 from
the old office-stream-only version, 14,479 of them naming a real team across
3,218 teams. 74% of the 25,410 IARs the SEC lists at CRD 7691. About 10 minutes.

Run:  python src/merrill_async.py
      python src/merrill_async.py --dry-run       fetch and report, write nothing
      python src/merrill_async.py --skip-teams    skip the team pages
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import random
import sys
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path, scratch_path  # one naming convention, defined once

import httpx
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRANCHES = ROOT / "data" / "output" / "advisor_branches.parquet"

ANSWERS = "https://liveapi.yext.com/v2/accounts/me/answers/vertical/query"
# The office stream the previous version used. Kept as a THIRD source, not as
# the primary one: it holds 301 people the searchable directory does not, so
# dropping it to fix the team-name bug would have traded one gap for another.
# It needs no Cloudflare clearance either -- a plain request answers 200, so
# the CDP session the old script built for it was never necessary.
STREAM = "https://cdn.yextapis.com/v2/accounts/me/content/linkedAdvisorsStream"
STREAM_KEY = "8b706720fbc39833239b317bdb403765"
MERRILL_CRD = "7691"
# Public key: served to every visitor of advisor.ml.com in the page bundle.
API_PARAMS = {
    "experienceKey": "merrill_answers",
    "api_key": "0d9b2553a63dd9c1a39224b5b7916fb4",
    "v": "20190101", "version": "PRODUCTION", "locale": "en",
    "verticalKey": "financial_professionals",
    "retrieveFacets": "false", "facetFilters": "{}", "sortBys": "[]",
    "context": "{}", "source": "STANDARD",
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
API_HEADERS = {"accept": "*/*", "accept-language": "en-US,en;q=0.9",
               "referer": "https://advisor.ml.com/", "user-agent": UA}
PAGE_HEADERS = {"user-agent": UA, "accept": "text/html,application/xhtml+xml"}

PAGE_SIZE = 50
OFFSET_CEILING = 10000

# Every region Merrill files an address in, plus the territories. A region the
# API knows and this list does not is still reached by the unfiltered pass, and
# any shortfall is printed, so a short list is visible rather than silent.
REGIONS = ["AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA",
           "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA",
           "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
           "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX",
           "UT", "VT", "VA", "WA", "WV", "WI", "WY", "PR", "VI", "GU", "AS",
           "MP"]

# The header the previous version wrote, preserved exactly: build_contacts.py
# maps "Team Name" and "Profile URL" by these strings. New fields are appended.
LEGACY = ["First Name", "Last Name", "Marketing Name", "Job Title", "Team Name",
          "Email", "Formatted Phone", "Office Center Code", "Street Line 1",
          "City", "State", "Postal Code", "Profile URL", "Image URL"]
ADDED = ["Advisor ID", "Entity Type", "Team Site", "Team Entity ID", "Other Teams",
         "Office Name", "NMLS", "Designations", "LinkedIn", "Latitude", "Longitude",
         "source"]


# --------------------------------------------------------------------------
# Phase 1 -- the Answers directory
# --------------------------------------------------------------------------
async def page(client, region, offset):
    """One page of results, with the vertical's total for this slice."""
    params = dict(API_PARAMS, input="", limit=str(PAGE_SIZE), offset=str(offset))
    if region:
        params["filters"] = json.dumps({"address.region": {"$eq": region}})
    for attempt in range(4):
        try:
            r = await client.get(ANSWERS, params=params, headers=API_HEADERS, timeout=45)
            if r.status_code == 200:
                body = r.json().get("response") or {}
                return body.get("results") or [], body.get("resultsCount") or 0
            # 429 and 5xx are worth another go. A 400 is a bad request and will
            # not improve by being repeated.
            if r.status_code < 500 and r.status_code != 429:
                print(f"    [-] HTTP {r.status_code} {region or 'all'}@{offset}: {r.text[:110]}")
                return [], 0
        except Exception as exc:
            print(f"    [-] {type(exc).__name__} {region or 'all'}@{offset}")
        await asyncio.sleep(1.5 * (2 ** attempt) + random.random())
    return [], 0


async def sweep(concurrency):
    """Every professional the directory will hand over, keyed on entity id."""
    people = {}
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(http2=False) as client:
        async def take(rows):
            for r in rows:
                data = r.get("data") or {}
                if data.get("id"):
                    people.setdefault(data["id"], data)

        async def collect(region):
            first, total = await page(client, region, 0)
            await take(first)
            if total <= PAGE_SIZE:
                return total
            reach = min(total, OFFSET_CEILING)

            async def one(off):
                async with sem:
                    rows, _ = await page(client, region, off)
                    await take(rows)
                    await asyncio.sleep(0.05)

            await asyncio.gather(*[one(o) for o in range(PAGE_SIZE, reach, PAGE_SIZE)])
            return total

        # Unfiltered first: it reaches 10,000 on its own and gives the
        # population figure everything else is measured against.
        published = await collect(None)
        print(f"    directory reports {published:,} professionals; "
              f"unfiltered paging reached {len(people):,}")

        for i, region in enumerate(REGIONS, start=1):
            before = len(people)
            total = await collect(region)
            if total >= OFFSET_CEILING:
                print(f"    [!] {region} alone reports {total:,}, at or past the "
                      f"offset ceiling -- that state needs splitting further")
            if len(people) > before or i % 12 == 0:
                print(f"    {i:>2}/{len(REGIONS)} {region} +{len(people) - before:<4} "
                      f"total {len(people):,}")
    return people, published


def picture_url(value):
    """c_profilePicture is sometimes {url} and sometimes {image:{url}}."""
    if not isinstance(value, dict):
        return ""
    inner = value.get("image")
    if isinstance(inner, dict) and inner.get("url"):
        return inner["url"]
    return value.get("url") or ""


def normalise(data, source="directory"):
    """One Answers record -> one roster row."""
    addr = data.get("address") or {}
    teams = [t for t in (data.get("c_teamNameAndSite") or []) if isinstance(t, dict)]
    # A professional may sit on more than one team. The first is the one their
    # own profile URL lives under, so it is the one that names them; the rest
    # are carried rather than dropped, because a second team is real.
    lead = teams[0] if teams else {}
    coord = data.get("cityCoordinate") or data.get("yextDisplayCoordinate") or {}
    emails = data.get("emails") or []
    slug = data.get("slug") or ""
    return {
        "First Name": data.get("c_advisorFirstName") or "",
        "Last Name": data.get("c_advisorLastName") or "",
        "Marketing Name": data.get("c_marketingName") or data.get("name") or "",
        "Job Title": data.get("c_jobTitle") or "",
        # The real team, not the office code the previous version put here.
        "Team Name": lead.get("teamName") or "",
        "Email": emails[0] if emails else "",
        "Formatted Phone": data.get("c_phoneNumberFormatted") or data.get("mainPhone") or "",
        "Office Center Code": data.get("c_officeCenterCode") or "",
        "Street Line 1": addr.get("line1") or "",
        "City": addr.get("city") or "",
        "State": addr.get("region") or "",
        "Postal Code": addr.get("postalCode") or "",
        "Profile URL": data.get("website") or (
            f"https://advisor.ml.com/{slug}" if slug else ""),
        "Image URL": picture_url(data.get("c_profilePicture")),
        "Advisor ID": data.get("id") or "",
        # ADVISOR or PROFESSIONAL_STAFF, straight from Merrill rather than
        # guessed from a job title. The team pages are mostly staff -- 25 of
        # the Westmoreland Group's 27 -- so this is the column to filter on if
        # the calling universe should be advisors only.
        # Upper-cased because the two sources disagree on capitalisation for
        # the same value: the directory returns "Advisor", the team pages
        # "ADVISOR". Left alone, a filter on one spelling silently drops 10,965
        # people or 2, depending which spelling it was written against.
        "Entity Type": (data.get("c_entityTypeCustomField") or
                        ("ADVISOR" if str(data.get("id") or "").startswith("a_") else "")).upper(),
        "Team Site": lead.get("teamSite") or "",
        "Team Entity ID": lead.get("teamEntityId") or "",
        "Other Teams": "; ".join(t.get("teamName") or "" for t in teams[1:] if t.get("teamName")),
        # The office is still worth keeping -- it was the only thing the old
        # file had. It is just not a team, so it gets its own column. The slug's
        # second segment is the office: sites/ga/atl-pw/westmorelandgroup.
        "Office Name": slug.split("/")[2] if slug.count("/") >= 2 else "",
        "NMLS": data.get("nmlsNumber") or "",
        "Designations": "; ".join(
            d.get("abbreviation") or "" for d in (data.get("c_designations") or [])
            if isinstance(d, dict) and d.get("abbreviation")),
        "LinkedIn": data.get("c_linkedInURL") or "",
        "Latitude": coord.get("latitude") or "",
        "Longitude": coord.get("longitude") or "",
        "source": source,
    }


# --------------------------------------------------------------------------
# Phase 2 -- team pages
# --------------------------------------------------------------------------
MEMBER_KEY = '"c_advisorFirstName"'


def embedded_members(html):
    """Team members out of a team page's embedded payload.

    The page ships its data as one percent-encoded blob inside a module script
    -- no __NEXT_DATA__ block, no fetchable endpoint -- so it is decoded whole
    and the member objects are brace-matched out of it. Ugly, and the
    alternative of scraping names out of rendered markup loses the email, the
    job title and the entity id, which are the fields that make a row worth
    having.
    """
    text = urllib.parse.unquote(html)
    out, at = [], 0
    while True:
        hit = text.find(MEMBER_KEY, at)
        if hit == -1:
            return out
        start = text.rfind("{", 0, hit)
        at = hit + len(MEMBER_KEY)
        if start == -1:
            continue
        depth, end = 0, -1
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == -1:
            continue
        try:
            obj = json.loads(text[start:end])
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("c_advisorFirstName"):
            out.append(obj)
            at = end


async def fetch_team(client, url, sem):
    async with sem:
        for attempt in range(3):
            try:
                r = await client.get(url, headers=PAGE_HEADERS, timeout=60,
                                     follow_redirects=True)
                if r.status_code == 200:
                    return r.text
                if r.status_code in (404, 410):
                    return ""
            except Exception:
                pass
            await asyncio.sleep(2 * (2 ** attempt) + random.random())
        return ""


async def sweep_teams(sites, concurrency):
    found, done = {}, 0
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(http2=False) as client:
        tasks = {url: asyncio.create_task(fetch_team(client, url, sem)) for url in sites}
        for url, task in tasks.items():
            html = await task
            done += 1
            if html:
                members = embedded_members(html)
                if members:
                    found[url] = members
            if done % 100 == 0 or done == len(tasks):
                print(f"    {done:>5}/{len(tasks)} team pages  "
                      f"{sum(len(v) for v in found.values()):,} people")
    return found


# --------------------------------------------------------------------------
# Phase 3 -- the office stream
# --------------------------------------------------------------------------
def stream_team(prof):
    """The team a stream record belongs to, from c_preferredLinkedTeam.

    NOT the enclosing document's `name`. That document is sometimes a team and
    sometimes an office -- its own c_entityTypeCustomField says which -- and
    reading `name` off it regardless is what put "Atl Pw" and "5av Fin Ctr" in
    the team column for 11,216 people.
    """
    teams = [t for t in (prof.get("c_preferredLinkedTeam") or []) if isinstance(t, dict)]
    if not teams:
        return "", ""
    site = (teams[0].get("websiteUrl") or {}).get("url") or ""
    return teams[0].get("name") or "", site


async def sweep_stream(known):
    """Everyone in the office stream who is not already accounted for, plus
    every team site it names.

    THE STREAM IS CAPPED AT 50,000 DOCUMENTS. It holds 65,088, and a page token
    past 50,000 is refused outright: error 9507, "The offset is too large.
    Please reduce the scope of the content endpoint." Retrying does not help,
    and there is nothing to reduce the scope WITH -- the endpoint accepts no
    filter but an exact `id`, and rejects `sortBy`, so the tail cannot be
    reached from the other end either. The shortfall is printed every run.

    Which is why the team sites are harvested here too: a team the directory
    never mentions still gets its page read, and the people on it recovered
    that way rather than through the part of the stream we cannot see.
    """
    found, sites, token, docs_seen, total = {}, {}, None, 0, 0
    async with httpx.AsyncClient(http2=False) as client:
        while True:
            params = {"api_key": STREAM_KEY, "v": "20230509", "limit": "50"}
            if token:
                params["pageToken"] = token
            try:
                r = await client.get(STREAM, params=params, headers=API_HEADERS, timeout=45)
            except Exception as exc:
                print(f"    [-] {type(exc).__name__} on the stream; stopping")
                break
            if r.status_code != 200:
                print(f"    [!] stream stopped at {docs_seen:,} of {total:,} documents "
                      f"(HTTP {r.status_code}); {total - docs_seen:,} not reachable")
                break
            body = r.json().get("response") or {}
            docs = body.get("docs") or []
            total = body.get("count") or total
            if not docs:
                break
            for doc in docs:
                docs_seen += 1
                for prof in doc.get("c_linkedFinancialProfessionals") or []:
                    team, site = stream_team(prof)
                    if site:
                        sites.setdefault(site, team)
                    pid = prof.get("id")
                    if not pid or pid in known or pid in found:
                        continue
                    found[pid] = (prof, doc)
            if docs_seen % 5000 < 50:
                print(f"    {docs_seen:,}/{total:,} documents  {len(found):,} new people, "
                      f"{len(sites):,} team sites")
            token = body.get("nextPageToken")
            if not token:
                print(f"    stream complete: {docs_seen:,} documents")
                break
            await asyncio.sleep(0.05)
    return found, sites


def from_stream(prof, doc):
    row = normalise(prof, source="office_stream")
    team, site = stream_team(prof)
    row["Team Name"] = team
    row["Team Site"] = site
    if not row["Profile URL"]:
        row["Profile URL"] = (prof.get("websiteUrl") or {}).get("url") or ""
    # The enclosing document names an OFFICE unless it says it is a team.
    if (doc.get("c_entityTypeCustomField") or "") != "TEAM":
        row["Office Name"] = row["Office Name"] or (doc.get("name") or "")
    return row


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    ap.add_argument("--skip-teams", action="store_true", help="skip the team pages")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--page-concurrency", type=int, default=8)
    args = ap.parse_args()

    started = time.time()
    print("[*] phase 1: Answers directory, swept per state")
    people, published = asyncio.run(sweep(args.concurrency))
    if not people:
        raise SystemExit("the directory returned nobody -- refusing to write an empty roster")
    rows = {pid: normalise(data) for pid, data in people.items()}
    print(f"[*] {len(rows):,} of the {published:,} the directory reports"
          + (f" ({len(rows) / published:.1%})" if published else ""))

    # Phase 2 BEFORE the team pages: the stream names teams the directory never
    # mentions, and those pages are worth reading too.
    print("[*] phase 2: office stream")
    extra, stream_sites = asyncio.run(sweep_stream(set(rows)))
    for pid, (prof, doc) in extra.items():
        rows[pid] = from_stream(prof, doc)
    print(f"[*] the office stream added {len(extra):,} people the directory does not list")

    sites, pages = {}, {}
    for row in rows.values():
        if row["Team Site"]:
            sites.setdefault(row["Team Site"], row["Team Name"])
    fresh = sum(1 for u in stream_sites if u not in sites)
    for url, team in stream_sites.items():
        sites.setdefault(url, team)
    if not args.skip_teams:
        print(f"[*] phase 3: {len(sites):,} distinct team sites "
              f"({fresh:,} of them named only by the stream)")
        pages = asyncio.run(sweep_teams(sites, args.page_concurrency))

    added = filled = 0
    for site, members in pages.items():
        for member in members:
            mid = member.get("id")
            if not mid:
                continue
            row = normalise(member, source="team_page")
            # The page does not repeat the team on each member; it is the page.
            row["Team Name"] = row["Team Name"] or sites.get(site, "")
            row["Team Site"] = row["Team Site"] or site
            if mid not in rows:
                rows[mid] = row
                added += 1
                continue
            # ENRICH rather than skip. The stream runs first now, so it claims
            # people whose records it holds thinly -- no team, no profile page.
            # Skipping them because the id was already present cost 6,178
            # profile URLs and 823 team names against the previous ordering,
            # which is a worse file arrived at by collecting more of them.
            existing = rows[mid]
            for field, value in row.items():
                if value and not existing.get(field) and field != "source":
                    existing[field] = value
                    filled += 1
    print(f"[*] team pages added {added:,} more and filled {filled:,} "
          f"blank fields on people already found")

    df = pd.DataFrame(list(rows.values()))
    for column in LEGACY + ADDED:
        if column not in df.columns:
            df[column] = ""
    df = df[LEGACY + ADDED].fillna("")
    print(f"[*] {len(df):,} rows in {time.time() - started:.0f}s")
    if args.dry_run:
        print("[*] dry run: nothing written")
        return

    scratch_path("merrill", "answers", ext="json").write_text(
        json.dumps(list(people.values()), indent=1), encoding="utf-8")
    out = roster_path("merrill")
    df.to_csv(out, index=False)
    print(f"    roster -> {out}")

    print(f"    {int((df['Email'] != '').sum()):,} have an email; "
          f"{int((df['Formatted Phone'] != '').sum()):,} a phone; "
          f"{int((df['Profile URL'] != '').sum()):,} a profile page")
    named = df["Team Name"].replace("", pd.NA)
    print(f"    {int(named.notna().sum()):,} name a team, across {named.nunique():,} teams; "
          f"{df['State'].replace('', pd.NA).nunique()} states")
    print("    by source: " + ", ".join(
        f"{k} {v:,}" for k, v in df["source"].value_counts().items()))

    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd", "branch_state"])
        b = b[b["firm_crd"].astype(str) == MERRILL_CRD]
        if len(b):
            iars = b["advisor_crd"].nunique()
            print(f"    SEC lists {iars:,} IARs at CRD {MERRILL_CRD}; this roster "
                  f"publishes {len(df):,} ({len(df) / iars:.0%})")


if __name__ == "__main__":
    main()
