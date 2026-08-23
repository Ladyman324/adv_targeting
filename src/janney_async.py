"""Janney Montgomery Scott advisor roster -> data/raw/firm_rosters/janney_<date>.csv

Two stages:

  discovery  GET /about/meet-janney/financial-advisors/<page>   (1..45, 20/page)
  enrich     GET /about/meet-janney/financial-advisors/<slug>

The trailing number in the URL you land on is a PAGE, not an advisor: /41 is
page 41 of 45. Paging is plain GET, and the run stops at the first page with no
cards rather than trusting a hard-coded count -- 45 was found by bisection and
the last page holds a single advisor.

THE EMAIL IS CLOUDFLARE-OBFUSCATED
----------------------------------
Same trick as Mariner, so the same rule applies: grepping for "mailto:" returns
zero and means nothing. Each profile carries

    <a href="/cdn-cgi/l/email-protection#107460757e7e507a717e7e75693e737f7d">
    <span class="__cf_email__" data-cfemail="f69286939898b69c979898938fd895999b">

which are two encodings of the SAME address, dpenn@janney.com. Both are hex
with a leading XOR key; the href form is decoded here and the data-cfemail
attribute is used as a fallback.

THE ADDRESS COMES FROM THE MAPS LINK
------------------------------------
The visible address is one run-on string, but the Google Maps link beside it
carries the same address already delimited:

    ...&query=40+Morris+Avenue,+Suite+200,Bryn+Mawr,PA,19010

Parsing that gives street, city, state and ZIP without guessing where the city
begins. The rendered text is kept as a fallback.

Also present: team name, team website, job titles (an advisor can have two),
and a personal LinkedIn. No coordinates -- placement goes through the ZIP.

Run:  python src/janney_async.py [--dry-run] [--refresh]
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
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path, scratch_path  # one naming convention, defined once

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRANCHES = ROOT / "data" / "output" / "advisor_branches.parquet"

BASE = "https://www.janney.com"
LIST = BASE + "/about/meet-janney/financial-advisors/{}"
JANNEY_CRD = "463"
HEADERS = {
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
}
PAUSE = 0.35
RETRIES = 3
MAX_LIST_PAGES = 120     # runaway guard; the real end is an empty page

PROFILE_MARKER = "jcom-banner--person"   # every real profile has it
CARD_RE = re.compile(r'jcom-person-card-name"><a href="([^"]+)">(.*?)</a>', re.S)
NAME_RE = re.compile(r'<h1 class="jcom-banner-heading"\s*>(.*?)</h1>', re.S)
TITLE_RE = re.compile(r'<p class="jcom-banner-person-title"\s*>(.*?)</p>', re.S)
CFHREF_RE = re.compile(r'email-protection#([0-9a-fA-F]+)')
CFATTR_RE = re.compile(r'data-cfemail="([0-9a-fA-F]+)"')
PHONE_RE = re.compile(r'href="tel:([^"]+)"')
LINKEDIN_RE = re.compile(r'href="(https?://[^"]*linkedin\.com/[^"]+)"')
TEAM_RE = re.compile(r'<address>\s*<h2>(.*?)</h2>', re.S)
MAPS_RE = re.compile(r'google\.com/maps/search/\?api=1&(?:amp;)?query=([^"]+)"')
STREETTEXT_RE = re.compile(r'jcom-team-banner-street-address"[^>]*>.*?>(.*?)</a>', re.S)
TEAMSITE_RE = re.compile(r'<p><a class="jcom-team-banner-link" href="([^"]+)"')
TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def text(fragment: str) -> str:
    return WS.sub(" ", htmllib.unescape(TAGS.sub(" ", fragment or ""))).strip()


def cf_email(encoded: str) -> str:
    """Cloudflare obfuscation: byte one is an XOR key, the rest are the address."""
    try:
        key = int(encoded[:2], 16)
        out = "".join(chr(int(encoded[i:i + 2], 16) ^ key)
                      for i in range(2, len(encoded), 2))
    except ValueError:
        return ""
    return out if "@" in out and " " not in out else ""


def request(session, url, expect: str = ""):
    """Returns the response, "gone" on 404, or None after RETRIES failures.

    `expect` is a marker the real page must contain. Status code and content
    type are NOT sufficient: under sustained load Janney returns HTTP 200 with
    well-formed text/html that has no profile in it. Accepting those produced 74
    completely blank rows in a first full run -- advisors who exist, fetch fine
    on a retry, and would simply have been missing."""
    for attempt in range(RETRIES):
        try:
            r = session.get(url, headers=HEADERS, timeout=90)
            if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                if not expect or expect in r.text:
                    return r
                print(f"    [-] attempt {attempt + 1}: 200 but no {expect!r} {url}")
                time.sleep(PAUSE * (2 ** attempt) + random.random())
                continue
            if r.status_code == 404:
                return "gone"
            print(f"    [-] attempt {attempt + 1}: HTTP {r.status_code} {url}")
        except Exception as exc:
            print(f"    [-] attempt {attempt + 1}: {type(exc).__name__} {exc}")
        time.sleep(PAUSE * (2 ** attempt) + random.random())
    return None


def split_address(query: str):
    """'40+Morris+Avenue,+Suite+200,Bryn+Mawr,PA,19010' -> parts.

    The Maps query is already delimited, which is why it is preferred over the
    rendered text: 'Bryn Mawr' has a space in it, so there is no reliable way to
    find where the street ends in the run-on version."""
    raw = urllib.parse.unquote_plus(query).strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) >= 3 and re.fullmatch(r"\d{5}(-\d{4})?", parts[-1]) \
            and re.fullmatch(r"[A-Z]{2}", parts[-2]):
        return " ".join(parts[:-3]), parts[-3], parts[-2], parts[-1]
    return raw, "", "", ""


COLUMNS = ["name", "title", "second_title", "email", "phone", "address1", "city",
           "state", "postal", "team_name", "team_website", "linkedin",
           "profile_url", "slug"]


def parse_profile(markup: str, url: str) -> dict:
    titles = [text(t) for t in TITLE_RE.findall(markup)]
    href = CFHREF_RE.search(markup)
    attr = CFATTR_RE.search(markup)
    email = cf_email(href.group(1)) if href else ""
    if not email and attr:                       # the two encodings agree; this
        email = cf_email(attr.group(1))          # is a belt-and-braces fallback
    phone = PHONE_RE.search(markup)
    team = TEAM_RE.search(markup)
    li = LINKEDIN_RE.search(markup)
    site = TEAMSITE_RE.search(markup)
    maps = MAPS_RE.search(markup)
    if maps:
        street, city, state, postal = split_address(maps.group(1))
    else:
        shown = STREETTEXT_RE.search(markup)
        street, city, state, postal = (text(shown.group(1)) if shown else ""), "", "", ""
    name = NAME_RE.findall(markup)
    return {
        "name": next((text(n) for n in name if text(n)), ""),
        "title": titles[0] if titles else "",
        "second_title": titles[1] if len(titles) > 1 else "",
        "email": email,
        "phone": phone.group(1) if phone else "",
        "address1": street,
        "city": city,
        "state": state,
        "postal": postal,
        "team_name": text(team.group(1)) if team else "",
        "team_website": site.group(1) if site else "",
        "linkedin": li.group(1) if li else "",
        "profile_url": url,
        "slug": url.rstrip("/").rsplit("/", 1)[-1],
    }


def discover(session, failures):
    urls, page = [], 1
    while page <= MAX_LIST_PAGES:
        r = request(session, LIST.format(page))
        if r is None:
            failures.append(f"list p{page}")
            break
        if r == "gone":
            break
        cards = CARD_RE.findall(r.text)
        if not cards:                      # the real end of the list
            break
        # Unescape the href. Seven advisors have an apostrophe in their slug --
        # casey-o'toole, robert-d'ambrosio, elizabeth-'beth'-young -- and the
        # markup carries it as &#39;. Requesting the escaped form returns
        # HTTP 500, so without this they are silently lost.
        urls += [htmllib.unescape(u) for u, _ in cards]
        print(f"[list {page}] {len(cards):>3} cards  |  total {len(urls):,}")
        page += 1
        time.sleep(PAUSE)
    return urls


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="discovery only")
    ap.add_argument("--refresh", action="store_true", help="ignore the cached discovery")
    ap.add_argument("--limit", type=int, help="only the first N profiles, for a trial")
    args = ap.parse_args()

    started = time.time()
    session = requests.Session()
    failures, gone = [], []

    cache = scratch_path("janney", "discovery", ext="json")
    if cache.exists() and not args.refresh:
        urls = json.loads(cache.read_text(encoding="utf-8"))
        print(f"[*] {len(urls):,} profile URLs from cache; --refresh to re-discover")
    else:
        urls = sorted(set(discover(session, failures)))
        if urls:
            cache.write_text(json.dumps(urls, indent=1), encoding="utf-8")

    if not urls:
        raise SystemExit("no profile URLs found -- refusing to write an empty roster")
    print(f"[*] discovery: {len(urls):,} advisors in {time.time() - started:.0f}s")
    if args.dry_run:
        return
    if args.limit:
        urls = urls[:args.limit]

    rows = []
    for i, url in enumerate(urls, 1):
        r = request(session, url, expect=PROFILE_MARKER)
        if r == "gone":
            gone.append(url)
            continue
        if r is None:
            failures.append(url)
            continue
        rows.append(parse_profile(r.text, url))
        if i % 100 == 0:
            print(f"    enriched {i:,}/{len(urls):,}")
        time.sleep(PAUSE)

    # Second pass over anything that still came back empty. Throttling is
    # bursty, so a URL that failed mid-run usually succeeds a minute later.
    blanks = [r for r in rows if not r["name"]]
    if blanks:
        print(f"[*] repairing {len(blanks)} blank profile(s)")
        for row in blanks:
            time.sleep(PAUSE * 3)
            rr = request(session, row["profile_url"], expect=PROFILE_MARKER)
            if rr not in (None, "gone"):
                row.update(parse_profile(rr.text, row["profile_url"]))
        still = sum(1 for r in rows if not r["name"])
        print(f"    {len(blanks) - still} recovered, {still} still blank")

    df = pd.DataFrame(rows, columns=COLUMNS)
    scratch_path("janney", "profiles", ext="json").write_text(
        json.dumps(rows, indent=1), encoding="utf-8")
    out = roster_path("janney")
    df.to_csv(out, index=False)

    emails = int((df["email"] != "").sum())
    print(f"\n[*] {len(df):,} advisors in {time.time() - started:.0f}s -> {out}")
    print(f"    {emails:,} have an email ({emails / len(df):.1%}, decoded from "
          f"Cloudflare obfuscation); {int((df['phone'] != '').sum()):,} a phone")
    print(f"    {int((df['address1'] != '').sum()):,} a street address; "
          f"{int((df['postal'] != '').sum()):,} a ZIP; no coordinates in this feed")
    print(f"    {df['state'].replace('', pd.NA).nunique()} states, "
          f"{df['team_name'].replace('', pd.NA).nunique()} teams, "
          f"{int((df['linkedin'] != '').sum()):,} a LinkedIn profile")

    blank = int((df["name"] == "").sum())
    if blank:
        print(f"    [!] {blank} profile(s) parsed with no name -- check the markup")
    unparsed = int(((df["address1"] != "") & (df["postal"] == "")).sum())
    if unparsed:
        print(f"    [!] {unparsed} address(es) had no Maps link to split -- street "
              f"kept whole, city/state/ZIP empty")
    dupes = len(df) - df["slug"].nunique()
    if dupes:
        print(f"    [!] {dupes} duplicate slug(s)")
    if gone:
        print(f"    {len(gone)} listed profile(s) 404 -- stale entries in Janney's "
              f"own index: {gone[:4]}")

    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == JANNEY_CRD]["advisor_crd"].nunique()
        print(f"    SEC lists {sec:,} IARs at CRD {JANNEY_CRD}; the directory "
              f"publishes {len(df):,} ({len(df) / sec:.0%})")
    if failures:
        print(f"    {len(failures)} request(s) FAILED after {RETRIES} attempts: "
              f"{failures[:5]}")


if __name__ == "__main__":
    main()
