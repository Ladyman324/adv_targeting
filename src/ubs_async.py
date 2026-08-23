"""UBS advisor roster -> data/raw/firm_rosters/ubs_<date>.csv

    POST https://presenter.broadridgeadvisor.com/locator/api/Search
    GET  https://advisors.ubs.com/<team>   and   https://local.ubs.com/<branch>

UBS's Find-an-Advisor page is a Broadridge "locator" tenant. The API needs no
cookie, no nonce and no browser -- plain HTTP with an Origin header is enough,
which is why this script has no Selenium, no Playwright and no CDP attachment.

THE API HAS NO FILTERS AND NO PAGING
------------------------------------
Every request returns a RANDOM 300 entities. Two consecutive calls overlap by
about a dozen records, and there is no cursor, no offset and no page number.
Filtering does not work either: a requirement option of ANY name -- PostalCode,
Region, City, LastName, the ParentMarketingName the site's own console shows --
comes back as the string "<msg />" rather than a result set.

That has a consequence worth stating plainly, because a previous attempt at
this swept 995 three-digit ZIP prefixes and believed it had harvested a
nationwide roster. Every one of those requests returned "<msg />", the parsing
guard was `if isinstance(data, dict)`, a string is not a dict, and so all 995
silently yielded nothing.

So the only lever is repetition, and the harvest is a coupon-collector problem:
draw 300 at random until the population stops producing anything new. The
previous working script drew a fixed 200 times and hoped. This one draws until
`--patience` consecutive requests add nobody, which stops when the data says to
rather than when a magic number runs out, and reports the saturation curve so a
short run is visibly short.

TEAM PAGES ARE NOT OPTIONAL
---------------------------
The locator lists advisors. It does NOT list everyone: client associates,
registered associates, analysts and some advisors appear only on their team's
own page. Those pages are static HTML keyed by entity id -- `data-entityid`
with matching `id="<eid>-displayname"`, `-jobtitle`, `-address`, `data-mail`
and `data-phone` -- so they parse without a browser too, and the entity ids are
the SAME namespace as the API's, which is what makes the merge safe.

Parsed structurally rather than by scraping addresses out of the text: every
UBS team page carries one boilerplate @ubs.com address in its footer, and a
regex over the page body adds that phantom person to every team in the firm.

WHAT IS AND IS NOT HERE
-----------------------
Present: name, job title, rank title, email, phone, office address, lat/lon,
team name and URL, branch (parent) name and URL, LinkedIn, photo, credentials.
Absent: CRD. Nothing in either source carries one, so these rows reach the
matcher on name and address like every other roster.

COLUMN NAMES ARE THE BROADRIDGE ONES ON PURPOSE
-----------------------------------------------
MarketingName, RankTitle, Emails, LocalNumber, Region and the rest are ugly,
and build_contacts.py's ROSTER_COLUMNS["ubs"] already maps them. Renaming them
to house style here would silently drop every UBS advisor out of contacts.json,
so the legacy header is preserved exactly and new fields are appended after it.

MEASURED 2026-08-20: 5,595 entities from the locator, 1,762 team and branch
sites. 175 of those sites list nobody on their root and keep their roster on a
/Meet-the-team.htm page instead, which is followed. 3,890 people appear only on
those pages. 9,485 rows -- 9,178 people and 307 branch offices -- against 5,589
rows from the locator alone. That is 98% of the 9,362 IARs the SEC lists at CRD
8174, and every state but the US Virgin Islands. Runtime about 12 minutes.

Run:  python src/ubs_async.py
      python src/ubs_async.py --dry-run            fetch and report, write nothing
      python src/ubs_async.py --skip-teams         locator only, much faster
"""
from __future__ import annotations

import argparse
import asyncio
import os
import json
import pathlib
import random
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path, scratch_path  # one naming convention, defined once

import httpx
import pandas as pd
from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRANCHES = ROOT / "data" / "output" / "advisor_branches.parquet"

SEARCH = "https://presenter.broadridgeadvisor.com/locator/api/Search"
UBS_CRD = "8174"
PAYLOAD = {"locator": "UBS",
           "parameter": {"request": {"requirement": [{"option": []}]}}}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json;charset=UTF-8",
    # The API authorises on Origin alone -- no cookie, no token. Both headers
    # are required; without them the endpoint answers 403.
    "Origin": "https://advisors.ubs.com",
    "Referer": "https://advisors.ubs.com/",
    "User-Agent": UA,
}
PAGE_HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}

# The legacy header, in its original order. Do not reorder or rename: see the
# module docstring.
LEGACY = ["Address1", "Address2", "AddressType", "City", "Country", "Emails",
          "EntityId", "EntitySiteId", "GeoLat", "GeoLon", "JobTitle",
          "LinkedInUrl", "LocalNumber", "MarketingName", "ParentEntityId",
          "ParentMarketingName", "ParentSiteUrl", "PostalCode", "RankTitle",
          "Region", "SiteIsLive", "SiteName", "TeamSiteNames", "TeamSiteUrls",
          "TitleDisplayOption", "UniqueId", "PhotoUrl", "Url", "FaxNumber",
          "SecondaryPhone", "Credentials", "AdditionalOfficeAddress",
          "GeoLat1", "GeoLon1", "TollFreeNumber"]
# Appended after the legacy block, so a consumer reading by name is unaffected.
ADDED = ["ProfileType", "source", "team_page_url"]

# UBS writes multi-valued attributes with "!" between them, NOT a comma or a
# semicolon. Splitting on ";" produces one unusable URL per advisor with two
# teams -- which is 195 of every 300 records, so it is the common case.
MULTI = "!"


# --------------------------------------------------------------------------
# Phase 1 -- the locator API
# --------------------------------------------------------------------------
async def draw(client: httpx.AsyncClient) -> list:
    """One request: a random 300 entities, or [] if the tenant said nothing."""
    try:
        r = await client.post(SEARCH, json=PAYLOAD, headers=API_HEADERS, timeout=60)
    except Exception as exc:
        print(f"    [-] {type(exc).__name__}: {exc}")
        return []
    if r.status_code != 200:
        print(f"    [-] HTTP {r.status_code}")
        return []
    try:
        body = r.json()
    except ValueError:
        return []
    # A STRING body is the tenant's way of saying "no result" -- literally
    # "<msg />". Checked explicitly rather than with a .get() on a dict guard,
    # because the silent version of this check is what made the ZIP sweep
    # look like it was working.
    if not isinstance(body, dict):
        return []
    return body.get("Entity") or []


async def harvest(concurrency: int, patience: int, max_calls: int) -> dict:
    """Draw until `patience` consecutive requests add nobody new."""
    seen: dict[str, dict] = {}
    curve, calls, barren = [], 0, 0
    async with httpx.AsyncClient(http2=False) as client:
        while barren < patience and calls < max_calls:
            wave = await asyncio.gather(*[draw(client) for _ in range(concurrency)])
            calls += concurrency
            added = 0
            for entity in [e for batch in wave for e in batch]:
                extra = entity.get("AdditionalData") or {}
                key = extra.get("EntityId") or entity.get("UniqueId")
                if key and key not in seen:
                    seen[key] = entity
                    added += 1
            curve.append(len(seen))
            barren = barren + concurrency if added == 0 else 0
            print(f"    {calls:>4} calls  +{added:<4} unique {len(seen):,}")
            await asyncio.sleep(0.15 + random.random() * 0.1)
    if calls >= max_calls:
        print(f"    [!] stopped at the --max-calls ceiling of {max_calls}, NOT at "
              f"saturation -- the roster is probably incomplete")
    return seen


def cell(value):
    """One JSON value -> one CSV cell.

    Emails arrives as a LIST for 75 of the 5,594 locator records -- a shared team
    mailbox alongside the advisor's own, or two people on one branch profile.
    Written as real JSON so build_contacts.parse_email_list() takes its
    json.loads() branch; Python's own repr uses single quotes, fails that
    parse, and falls through to a regex split that leaves the quotes and commas
    attached to the addresses.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return json.dumps([str(v).strip() for v in value if str(v).strip()])
    return value


def tidy_title(value: str) -> str:
    """Collapse a title Broadridge has repeated.

    17 locator records carry "Financial Advisor, Financial Advisor, Financial
    Advisor, Financial Advisor" -- one repeat per registration, by the look of
    it. That is their data rather than a parsing fault here, but it reaches the
    panel as the caption under a person's name, so it is squashed on the way
    in. Order is preserved and genuinely different titles are kept.
    """
    parts = [p.strip() for p in str(value or "").split(",") if p.strip()]
    return ", ".join(dict.fromkeys(parts))


def flatten(entity: dict) -> dict:
    """One entity -> one flat row, in Broadridge's own field names."""
    addresses = entity.get("Addresses") or []
    home = addresses[0] if addresses and isinstance(addresses[0], dict) else {}
    row = {k: cell(v) for k, v in home.items()}
    row.update({k: cell(v) for k, v in (entity.get("AdditionalData") or {}).items()})
    row["ProfileType"] = entity.get("ProfileType") or ""
    row["source"] = "locator"
    row["team_page_url"] = ""
    # MarketingName arrives padded ("  Henry A. Hernandez ") often enough to
    # matter: it is the name the matcher joins on.
    if row.get("MarketingName"):
        row["MarketingName"] = " ".join(str(row["MarketingName"]).split())
    for field in ("JobTitle", "RankTitle"):
        if row.get(field):
            row[field] = tidy_title(row[field])
    return row


# --------------------------------------------------------------------------
# Phase 2 -- team and branch pages
# --------------------------------------------------------------------------
TAGS = re.compile(r"<[^>]+>")
DISPLAY_ID = re.compile(r"^\d+-displayname$")
# What a UBS site calls the page its people are on, where that is not the root.
TEAM_LINK = re.compile(r"(meet-?the-?team|our-?team|the-?team)\.htm", re.I)


def absolute(url: str) -> str:
    """Broadridge stores these protocol-relative: //advisors.ubs.com/foo."""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http"):
        return url
    return "https://" + url.lstrip("/")


def site_urls(rows: list[dict]) -> dict[str, str]:
    """Every distinct team and branch page named by the locator.

    Branch pages are included alongside team pages because a branch office's
    own page lists its staff too, and an advisor with no team belongs to one.
    """
    out: dict[str, str] = {}
    for row in rows:
        for field in ("TeamSiteUrls", "ParentSiteUrl"):
            for part in str(row.get(field) or "").split(MULTI):
                url = absolute(part)
                if url:
                    out.setdefault(url, field)
    return out


def parse_members(html: str) -> list[dict]:
    """People on one team or branch page, keyed by the entity id in the markup.

    Parsed with an HTML parser rather than regular expressions, because UBS
    ships at least two card templates and a regex that fits one silently
    mis-reads the other. On the /Meet-the-team.htm template the name is an <h2>
    where the compact template uses <h3>, and the job title is followed by an
    unclosed bio paragraph -- so a pattern ending at `</h3>` or `</p>` ran on
    past the element and captured several hundred words of biography as
    somebody's job title.

    Deduplicated on entity id: a page carries desktop, mobile and print copies
    of the same card, all with the SAME element ids, so a naive walk returns
    every person three times.
    """
    soup = BeautifulSoup(html, "lxml")
    people, seen = [], set()
    for node in soup.find_all(id=DISPLAY_ID):
        eid = node.get("id", "").split("-")[0]
        if not eid or eid in seen:
            continue
        name = " ".join(node.get_text(" ", strip=True).split())
        if not name:
            continue
        seen.add(eid)

        def text(suffix: str) -> str:
            el = soup.find(id=f"{eid}-{suffix}")
            return " ".join(el.get_text(" ", strip=True).split()) if el else ""

        anchor = soup.find("a", attrs={"data-entityid": eid, "data-mail": True})
        phone_el = soup.find(id=f"{eid}-primaryphone")
        phone_tag = phone_el.find(attrs={"data-phone": True}) if phone_el else None
        address = soup.find(id=f"{eid}-address")
        street, city, region, postal = split_address(
            address.decode_contents() if address else "")
        people.append({
            "EntityId": eid, "MarketingName": name,
            "JobTitle": text("jobtitle"),
            # RankTitle exists on the team templates too and the regex version
            # never looked for it, so every person the pages added arrived
            # without the seniority line the locator records all carry.
            "RankTitle": text("ranktitle"),
            "Emails": anchor["data-mail"] if anchor else "",
            "LocalNumber": phone_tag["data-phone"] if phone_tag else "",
            "Address1": street, "City": city, "Region": region, "PostalCode": postal,
            "GeoLat": (address.get("data-lat") or "") if address else "",
            "GeoLon": (address.get("data-lon") or "") if address else "",
            "AddressType": "Office", "Country": "USA",
            "ProfileType": "Individual", "source": "team_page",
        })
    return people


KEY = re.compile(r"[^a-z0-9]")


def best_name(url: str, names: list[str]) -> str:
    """Which of several candidate names belongs to THIS site.

    Position cannot decide it. Where the names list carries an extra entry it
    is usually the lead advisor's own name, and that entry sometimes leads --
    "Marcos Douer !The Madison Group" -- and sometimes trails: "Klinger Quan
    Group !Jeffrey Klinger". Aligning from either end is right for one shape
    and wrong for the other, and both shapes are in the data.

    The URL slug settles it instead: /klingerquangroup matches "Klinger Quan
    Group" and not "Jeffrey Klinger"; /the-madison-group matches "The Madison
    Group" and not "Marcos Douer". Where the slug is initials and resembles
    nothing -- /hkls for Ascension Wealth Advisors -- the LAST candidate is
    taken, which is the shape of every such record seen here.
    """
    slug = KEY.sub("", url.rsplit("/", 1)[-1].lower())
    if not slug:
        return names[-1] if names else ""
    best, score = "", 0
    for name in names:
        key = KEY.sub("", name.lower())
        if not key:
            continue
        if slug in key or key in slug:
            hit = 3
        else:
            common = len(os.path.commonprefix([slug, key]))
            hit = 2 if common >= 5 else 0
        if hit >= score:                       # >=, so a later match wins a tie
            best, score = name, hit
    return best if score else (names[-1] if names else "")


def team_names(rows: list[dict]) -> dict[str, str]:
    """site url -> the team's published name.

    TeamSiteNames and TeamSiteUrls are parallel "!"-delimited lists, and for 34
    of the 3,674 records with team fields they are NOT the same length -- the
    names list carries an extra entry, usually a person's name ahead of the
    team's ("Marcos Douer !The Madison Group" against one URL). Zipping those
    two lists positionally pairs the first name with the first URL, so
    /the-madison-group was named "Marcos Douer" and, where the extra sat in the
    middle, a whole team took its neighbour's name: everyone at Atlas Financial
    Group was captioned "The Provision Group".

    So this is two passes. Well-formed records -- equal lengths -- decide every
    name they can. Only then are the ragged ones used, and only to fill a site
    no clean record covered, aligned from the END on the reading that the extra
    entry leads. A site nothing can name stays blank rather than guessing.
    """
    tidy = lambda value: " ".join(str(value or "").split())

    def parts(row):
        names = [tidy(x) for x in str(row.get("TeamSiteNames") or "").split(MULTI)]
        urls = [absolute(x) for x in str(row.get("TeamSiteUrls") or "").split(MULTI)]
        return [x for x in names if x], [x for x in urls if x]

    out: dict[str, str] = {}
    ragged = []
    for row in rows:
        names, urls = parts(row)
        if not urls:
            continue
        if len(names) == len(urls):
            for name, url in zip(names, urls):
                out.setdefault(url, name)
        else:
            ragged.append((names, urls))

    for names, urls in ragged:
        # "R3 Wealth Management !R3 Wealth Management" against one URL: the
        # repeat is not a second team.
        #
        # Applied ONLY here, never before the length test. One team legitimately
        # runs several sites under one name -- Paramount Wealth Management
        # Partners has two, Laureate has four -- and collapsing those first
        # turns well-formed records into ragged ones and loses 20 names that
        # were already right.
        names = [x for i, x in enumerate(names) if i == 0 or x != names[i - 1]]
        for url in urls:
            if url not in out:
                pick = best_name(url, names)
                if pick:
                    out[url] = pick

    # A branch office is a team of sorts, and it is the only name an advisor
    # with no team has. Last, so a real team always wins.
    for row in rows:
        url, name = absolute(row.get("ParentSiteUrl")), tidy(row.get("ParentMarketingName"))
        if url and name:
            out.setdefault(url, name)
    return out


def roster_subpages(html: str, base: str) -> list[str]:
    """Team pages that hold their roster on a SEPARATE page.

    advisors.ubs.com/advocatepartners lists nobody; its twelve people --
    including four of the locator's own advisors -- are on
    /advocatepartners/Meet-the-team.htm. Both templates are in use across the
    firm, so the site root is followed one level to whatever it calls its team
    page rather than assuming either shape.
    """
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not TEAM_LINK.search(href) or href.startswith(("http", "//", "#")):
            continue
        out.append(base.rstrip("/") + "/" + href.lstrip("/"))
    return list(dict.fromkeys(out))


def split_address(block: str) -> tuple[str, str, str, str]:
    """"681 East Lake Street<br>Wayzata, MN 55391" -> its four parts."""
    parts = [" ".join(TAGS.sub(" ", chunk).split())
             for chunk in re.split(r"<br\s*/?>", block or "")]
    parts = [p for p in parts if p]
    if not parts:
        return "", "", "", ""
    tail = parts[-1]
    m = re.match(r"^(.*?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", tail)
    if m:
        return " ".join(parts[:-1]), m.group(1), m.group(2), m.group(3)
    return " ".join(parts), "", "", ""


async def fetch_page(client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore) -> str:
    async with sem:
        for attempt in range(3):
            try:
                r = await client.get(url, headers=PAGE_HEADERS, timeout=45,
                                     follow_redirects=True)
                if r.status_code == 200:
                    return r.text
                if r.status_code in (404, 410):
                    return ""
            except Exception:
                pass
            await asyncio.sleep(1.5 * (2 ** attempt) + random.random())
        return ""


async def sweep_sites(urls: dict[str, str], concurrency: int) -> dict[str, list]:
    """Fetch every site root, then follow the ones that keep their roster on a
    separate page. Two passes rather than one, because which template a site
    uses is only knowable after reading it."""
    found = {}
    sem = asyncio.Semaphore(concurrency)

    async def pass_over(targets: dict[str, str], label: str) -> dict[str, str]:
        """Returns the HTML of everything fetched, so the caller can look for
        sub-pages without downloading anything twice."""
        html_by_url, done = {}, 0
        async with httpx.AsyncClient(http2=False) as client:
            tasks = {url: asyncio.create_task(fetch_page(client, url, sem))
                     for url in targets}
            for url, task in tasks.items():
                html = await task
                done += 1
                if html:
                    html_by_url[url] = html
                    members = parse_members(html)
                    if members:
                        found[url] = members
                if done % 100 == 0 or done == len(tasks):
                    print(f"    {label} {done:>5}/{len(tasks)} pages  "
                          f"{sum(len(v) for v in found.values()):,} people")
        return html_by_url

    roots = await pass_over(urls, "roots ")

    # Only the sites that yielded nobody are followed. A root that already
    # listed its team has no second page worth fetching, and following every
    # site's team link regardless would double the request count for nothing.
    second: dict[str, str] = {}
    for url, html in roots.items():
        if found.get(url):
            continue
        for sub in roster_subpages(html, url):
            second.setdefault(sub, url)
    if second:
        print(f"    {len(second):,} sites list nobody on the root; following "
              f"their team page")
        await pass_over(second, "team  ")
    return found


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    ap.add_argument("--skip-teams", action="store_true", help="locator only")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--page-concurrency", type=int, default=10)
    ap.add_argument("--patience", type=int, default=24,
                    help="stop after this many consecutive requests add nobody")
    ap.add_argument("--max-calls", type=int, default=1200, help="hard ceiling")
    args = ap.parse_args()

    started = time.time()
    print("[*] phase 1: locator API, drawing until saturation")
    entities = asyncio.run(harvest(args.concurrency, args.patience, args.max_calls))
    if not entities:
        raise SystemExit("the locator returned nothing -- refusing to write an empty roster")
    rows = [flatten(e) for e in entities.values()]
    print(f"[*] {len(rows):,} entities from the locator")

    pages: dict[str, list] = {}
    if not args.skip_teams:
        urls = site_urls(rows)
        print(f"[*] phase 2: {len(urls):,} distinct team and branch pages")
        pages = asyncio.run(sweep_sites(urls, args.page_concurrency))

    # Merge. The locator record wins where both have the person: it carries
    # RankTitle, LinkedIn, the site id and the parent branch, none of which
    # appear in the page markup. A team page only ever ADDS people, and fills
    # the team columns for the ones it adds.
    by_id = {r["EntityId"]: r for r in rows if r.get("EntityId")}
    team_name = team_names(rows)
    added = 0
    for url, members in pages.items():
        # A roster reached at .../Meet-the-team.htm belongs to the SITE, not to
        # that file: the locator names teams by their site root, so the lookup
        # and the stored TeamSiteUrls both have to drop the page.
        site = re.sub(r"/[^/]+\.html?$", "", url)
        for person in members:
            if person["EntityId"] in by_id:
                continue
            person["team_page_url"] = url
            person["TeamSiteUrls"] = site.replace("https:", "")
            person["TeamSiteNames"] = team_name.get(site, "")
            by_id[person["EntityId"]] = person
            added += 1
    print(f"[*] team pages added {added:,} people the locator does not list")

    df = pd.DataFrame(list(by_id.values()))
    for column in LEGACY + ADDED:
        if column not in df.columns:
            df[column] = ""
    extras = [c for c in df.columns if c not in LEGACY + ADDED]
    df = df[LEGACY + ADDED + sorted(extras)].fillna("")

    people = df[df["ProfileType"] != "Branch"]
    print(f"[*] {len(df):,} rows in {time.time() - started:.0f}s "
          f"({len(people):,} people, {len(df) - len(people):,} branch offices)")
    if args.dry_run:
        print("[*] dry run: nothing written")
        return

    scratch_path("ubs", "locator", ext="json").write_text(
        json.dumps(list(entities.values()), indent=1), encoding="utf-8")
    out = roster_path("ubs")
    df.to_csv(out, index=False)
    print(f"    roster -> {out}")

    print(f"    {int((df['Emails'] != '').sum()):,} have an email; "
          f"{int((df['LocalNumber'] != '').sum()):,} a phone; "
          f"{int((df['GeoLat'] != '').sum()):,} carry coordinates")
    print(f"    {df['Region'].replace('', pd.NA).nunique()} states, "
          f"{int((df['TeamSiteNames'] != '').sum()):,} name a team, "
          f"{int((df['LinkedInUrl'] != '').sum()):,} a LinkedIn profile")
    print("    by source: " + ", ".join(
        f"{k} {v:,}" for k, v in df["source"].value_counts().items()))

    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd", "branch_state"])
        b = b[b["firm_crd"].astype(str) == UBS_CRD]
        if len(b):
            iars = b["advisor_crd"].nunique()
            print(f"    SEC lists {iars:,} IARs at CRD {UBS_CRD}; this roster publishes "
                  f"{len(people):,} people ({len(people) / iars:.0%})")
            missing = sorted(set(b["branch_state"].dropna()) - set(df["Region"]))
            if missing:
                print(f"    [!] {len(missing)} state(s) where UBS files a branch have "
                      f"no published advisor: {' '.join(missing)}")


if __name__ == "__main__":
    main()
