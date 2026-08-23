"""Alex. Brown branch team pages -> data/raw/firm_rosters/alex_brown_<date>.csv

WHY THIS EXISTS
---------------
Alex. Brown is a division of Raymond James (CRD 705, the RJA employee channel),
carried over from the 2016 Deutsche Bank US private client acquisition. Its
advisors are already IN the `raymond_james` roster -- they are simply not
LABELLED there, so there is no way to ask "show me the Alex. Brown book".

    branch_is_alex_brown  exists as a column in the advisor-search API response
                          and is 0 for all 8,400 rows. The field is returned and
                          never populated. It cannot be used, and this module is
                          the reason we no longer try.

The usable signal is the EMAIL DOMAIN. 232 advisors across the two existing RJ
rosters hold an @alexbrown.com address; this source raises that and, more
importantly, attaches a BRANCH and a TITLE to each one.

WHAT THIS ADDS OVER THE ROSTERS WE ALREADY HAVE (measured, not assumed)
----------------------------------------------------------------------
    235 people with an email across 15 branches
    169 already held by raymond_james or rj_branches
     66 NET NEW

The 66 are mostly branch leadership and support staff -- Complex Manager,
Branch Administrative Manager, Client Service Associate -- who never appear in
the advisor-search API because they are not registered advisors. `role` sorts
them from the 95 Client Advisors so a caller can take either, and the default
downstream should be advisors only.

TWO SURFACES, AND WHY THIS MODULE ONLY SCRAPES ONE
--------------------------------------------------
Branches are being migrated off the legacy site onto raymondjames.com:

    alexbrownbranches.com/<slug>/our-team.asp     open, no WAF, rich microdata
    raymondjames.com/<slug>-branch/about-us/...   Akamai, 403s every library

A migrated branch 301s from the first to the second. This module RECORDS the
redirect and stops there. It does not follow, because the far side is exactly
the surface src/rj_branches.py already owns through browser automation at one
request per minute -- chasing it here would earn the stateful Akamai penalty
described in that module's docstring and break both scrapers at once.

    migrated so far: annapolis, los-angeles, philadelphia

DISCOVERY: THE SITEMAP IS NOT THE BRANCH LIST
---------------------------------------------
sitemap.xml lists 16 branches and is stale in both directions -- it omits
annapolis and portland, and still lists los-angeles and philadelphia, which
have migrated. Slugs are also not derivable from city names: `portland` is
Portland MAINE, and the RJ side of the house spells Boca Raton `bocaraton`.

`experienced-advisors.asp` is the discovery oracle: it keeps serving 200 on the
legacy site even for branches whose team page has already migrated, so it finds
branches that `our-team.asp` alone would miss. `--discover` re-probes a
candidate list against it; BRANCHES below is the pinned result of that probe
(199 candidates tried, 16 live). Re-run it when a branch is suspected missing.

ONE MAILTO PER CARD
-------------------
Inherited verbatim from src/rj_branches.py, which learned it the hard way: a
parser that walked a fixed number of DOM levels paired one person's email with
another person's name on Atlanta's grid layout. A card holding zero or two
mailto links yields a row with a BLANK email rather than a guessed one. 7 of
242 people currently have no published address; they are kept, without one.

NOTE that the address is not always the name: Atlanta's Garrett Mutz publishes
charles.mutz@alexbrown.com. That is a display-name-vs-filed-name divergence of
exactly the kind src/reconcile_display_names.py adjudicates, and it is why the
email is carried through verbatim rather than being "corrected" to match.

NO DIRECT DIALS HERE
--------------------
Unlike the branch pages on raymondjames.com, these cards publish no personal
phone. The only number on the page is the branch switchboard from the
PostalAddress microdata, so every row is labelled phone_kind="switchboard".
That is the source, not a parsing defect -- and it means this file must never
be preferred over rj_branches for phone.

Run:  python src/alex_brown_async.py
      python src/alex_brown_async.py --discover
      python src/alex_brown_async.py --dry-run
"""
from __future__ import annotations

import argparse
import pathlib
import random
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path  # one naming convention, defined once

import pandas as pd
import requests
from bs4 import BeautifulSoup, NavigableString, Tag

# These pages carry en-dashes and registered-trademark signs in city names and
# designations ("World Financial Center - New York", "CRPS(R)"). On a Windows
# console that is cp1252 and the run dies mid-scrape on a PRINT, after the
# fetches have already been paid for. Never let a progress line kill a scrape.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

BASE = "https://www.alexbrownbranches.com"
TEAM = BASE + "/%s/our-team.asp"
ORACLE = BASE + "/%s/experienced-advisors.asp"
RJA_CRD = "705"

# Pinned result of --discover. Not the sitemap; see the docstring.
BRANCHES = [
    "annapolis", "atlanta", "baltimore", "boston", "chicago", "dallas",
    "greenwich", "houston", "lower-manhattan", "miami", "palm-beach",
    "park-avenue", "portland", "san-francisco", "washington", "winston-salem",
]

# Candidates for --discover. Wide on purpose: a slug that 404s costs one
# request, a branch we never think to try costs the whole office.
CANDIDATES = BRANCHES + """
los-angeles philadelphia new-york naples tampa denver seattle charlotte
nashville austin san-diego phoenix minneapolis detroit cleveland pittsburgh
st-louis kansas-city sacramento orlando jacksonville richmond raleigh
birmingham memphis columbus indianapolis milwaukee salt-lake-city las-vegas
hartford princeton short-hills stamford westport bethesda mclean wilmington
red-bank morristown summit boca-raton bocaraton fort-lauderdale sarasota
vero-beach greenville columbia savannah charleston louisville cincinnati
garden-city gardencity greensboro vienna hunt-valley devon florham-park
conshohocken coral-gables beverly-hills beverlyhills friendship-heights
friendshipheights st-petersburg carillon towson easton
""".split()

# Titles that mean "registered advisor" rather than branch staff. Matched as a
# substring on a lowercased title, so "Vice President, Client Advisor" and
# "Managing Director, Senior Institutional Consultant" both land as advisor.
ADVISOR_TITLES = (
    "client advisor", "financial advisor", "private wealth advisor",
    "wealth management", "investments", "invesments",  # sic, as published
    "institutional consultant", "managing director", "vice president",
    "director", "financial planning",
)
# Checked FIRST -- these outrank the words above, because "Director, Complex
# Administrative Manager" is branch staff despite starting with "Director".
SUPPORT_TITLES = (
    "administrative", "operations", "manger",  # sic, as published
    "service associate", "sales associate", "coordinator", "regulatory",
    "marketing", "analyst", "complex manager", "branch manager",
    "regional executive", "sales development",
)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
}


def role_of(title: str) -> str:
    """advisor | support | unknown -- see ADVISOR_TITLES for why order matters."""
    text = (title or "").lower()
    if not text:
        return "unknown"
    for word in SUPPORT_TITLES:
        if word in text:
            return "support"
    for word in ADVISOR_TITLES:
        if word in text:
            return "advisor"
    return "unknown"


def branch_address(soup: BeautifulSoup) -> dict:
    """The PostalAddress microdata block. Every branch page carries exactly one."""
    box = soup.select_one("[itemtype*='PostalAddress']")
    if not box:
        return {}

    def prop(name: str) -> str:
        el = box.select_one("[itemprop='" + name + "']")
        return " ".join(el.get_text(" ", strip=True).split()) if el else ""

    # addressLocality is not always a locality. Lower Manhattan publishes
    # "World Financial Center - New York": the building, then a dash, then the
    # actual city. Geocoding the whole string finds nothing, so keep the tail
    # and preserve what was dropped rather than discarding it silently.
    locality = prop("addressLocality")
    building = ""
    parts = re.split(r"\s+[–—-]\s+", locality, maxsplit=1)
    if len(parts) == 2:
        building, locality = parts[0].strip(), parts[1].strip()

    # addressRegion is a two-letter code everywhere except Washington, which
    # publishes "D.C.".
    region = prop("addressRegion").strip()
    if region.replace(".", "").upper() == "DC":
        region = "DC"

    return {
        "branch_street": prop("streetAddress"),
        "branch_building": building,
        "city": locality,
        "state": region,
        "zip": prop("postalCode"),
        "branch_phone": prop("telephone"),
    }


def people(soup: BeautifulSoup) -> list:
    """One row per schema.org/Person card. Blank email beats a guessed one."""
    out = []
    for card in soup.select("[itemtype*='schema.org/Person']"):
        heading = card.find("h5")
        if not heading:
            continue

        # Designations sit in their own <span> inside the heading. Pull them out
        # before reading the name, or "Lee Haverstock, CRPS" becomes the name.
        desig = " ".join(s.get_text(" ", strip=True)
                         for s in heading.find_all("span"))
        for span in heading.find_all("span"):
            span.decompose()
        name = " ".join(heading.get_text(" ", strip=True).split())
        name = name.rstrip(",").strip()

        # Title is the first non-empty text after the heading, stopping at the
        # icon row. The heading is sometimes wrapped in a link to the bio.
        anchor = heading.find_parent("a") or heading
        title = ""
        for sib in anchor.next_siblings:
            if isinstance(sib, NavigableString):
                text = " ".join(str(sib).split())
                if text:
                    title = text
                    break
            elif isinstance(sib, Tag):
                if sib.name == "br":
                    continue
                if sib.name == "a":
                    break
                text = sib.get_text(" ", strip=True)
                if text:
                    title = text
                    break

        mails = [a["href"][7:].split("?")[0]
                 for a in card.select("a[href^='mailto:']")]

        links = {}
        for a in card.select("a[href^='http']"):
            href = a["href"].strip()
            if "linkedin.com" in href:
                links["linkedin_url"] = href
            elif "alexbrown.com" in href or "raymondjames.com" in href:
                links.setdefault("website_url", href)
        bio = card.select_one("a[href^='biography.asp']")

        row = {
            "name": name,
            "title": title,
            "designations": desig.strip(" ,"),
            "role": role_of(title),
            "email": mails[0] if len(mails) == 1 else "",
            "email_count": len(mails),
            "bio_url": bio["href"] if bio else "",
        }
        row.update(links)
        out.append(row)
    return out


def fetch(url: str, session: requests.Session) -> requests.Response:
    resp = session.get(url, timeout=30, headers=HEADERS, allow_redirects=False)
    # The server sends `Content-Type: text/html` with NO charset, so requests
    # falls back to its ISO-8859-1 default while the page itself declares
    # <meta charset="utf-8">. Left alone, every non-ASCII character arrives as
    # mojibake -- "Center a<80><93> New York", "CRPS a<80><A0>" -- and it lands
    # in NAMES, which is the one field that has to be right. Pin it.
    resp.encoding = "utf-8"
    return resp


def discover(session: requests.Session) -> list:
    """Probe CANDIDATES against the oracle page. See the docstring."""
    live = []
    for slug in sorted(set(CANDIDATES)):
        try:
            resp = fetch(ORACLE % slug, session)
        except requests.RequestException as exc:
            print("  %-20s ERR %s" % (slug, str(exc)[:50]))
            continue
        # A real branch page is ~27KB; a soft 404 is far smaller.
        if resp.status_code == 200 and len(resp.text) > 5000:
            live.append(slug)
            print("  %-20s live" % slug)
        time.sleep(random.uniform(0.3, 0.8))
    return live


def scrape(slugs: list, session: requests.Session):
    rows, migrated = [], []
    for slug in slugs:
        try:
            resp = fetch(TEAM % slug, session)
        except requests.RequestException as exc:
            print("  %-20s ERR %s" % (slug, str(exc)[:50]))
            continue
        if resp.status_code in (301, 302):
            # Migrated to raymondjames.com. Recorded, deliberately not followed.
            migrated.append((slug, resp.headers.get("Location", "")))
            print("  %-20s MIGRATED -> rj_branches.py" % slug)
            continue
        if resp.status_code != 200:
            print("  %-20s HTTP %s" % (slug, resp.status_code))
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        addr = branch_address(soup)
        found = people(soup)
        for person in found:
            person.update(addr)
            person["branch_slug"] = slug
            person["branch_url"] = TEAM % slug
        rows += found
        print("  %-20s %3d people  %s, %s"
              % (slug, len(found), addr.get("city", "?"), addr.get("state", "?")))
        time.sleep(random.uniform(0.5, 1.2))
    return pd.DataFrame(rows), migrated


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--discover", action="store_true",
                    help="re-probe CANDIDATES for live branches and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="scrape but do not write")
    args = ap.parse_args()

    session = requests.Session()

    if args.discover:
        print("probing %d candidate slugs..." % len(set(CANDIDATES)))
        live = discover(session)
        print("\nlive: %d" % len(live))
        print("BRANCHES = %r" % (sorted(live),))
        missing = sorted(set(live) - set(BRANCHES))
        stale = sorted(set(BRANCHES) - set(live))
        if missing:
            print("NOT IN BRANCHES (add them): %s" % ", ".join(missing))
        if stale:
            print("IN BRANCHES BUT DEAD: %s" % ", ".join(stale))
        return 0

    print("scraping %d branches..." % len(BRANCHES))
    df, migrated = scrape(BRANCHES, session)
    if df.empty:
        print("no rows -- refusing to write an empty roster")
        return 1

    df["firm_crd"] = RJA_CRD
    df["firm"] = "Alex. Brown"
    # The page publishes no personal line; see the docstring.
    df["phone"] = df["branch_phone"]
    df["phone_digits"] = df["phone"].fillna("").str.replace(r"\D", "", regex=True)
    df["phone_kind"] = "switchboard"

    cols = ["branch_slug", "name", "title", "designations", "role", "email",
            "email_count", "phone", "phone_digits", "phone_kind",
            "branch_street", "branch_building", "city", "state", "zip",
            "branch_phone",
            "linkedin_url", "website_url", "bio_url", "branch_url",
            "firm", "firm_crd"]
    df = df.reindex(columns=cols)

    print("\n  people           %d" % len(df))
    print("  with email       %d" % int((df["email"] != "").sum()))
    print("  no mailto        %d" % int((df["email_count"] == 0).sum()))
    print("  two+ mailto      %d  (email left blank)"
          % int((df["email_count"] > 1).sum()))
    print("  role: " + ", ".join("%s %d" % (k, v)
                                 for k, v in df["role"].value_counts().items()))
    if migrated:
        print("  migrated to raymondjames.com (handled by rj_branches.py):")
        for slug, dest in migrated:
            print("      %-18s %s" % (slug, dest))

    if args.dry_run:
        print("\ndry run -- nothing written")
        return 0

    out = roster_path("alex_brown")
    df.to_csv(out, index=False, encoding="utf-8")
    print("\nwrote %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
