"""Stifel advisor roster -> data/raw/firm_rosters/stifel_<date>.csv

Two stages, because the two pages carry different things:

  discovery  POST /fa/search?state=<st>  -> the profile URL of every advisor
  enrich     GET  /fa/<slug>             -> title, phones, FULL street address,
                                            branch, website, LinkedIn

The search results carry only "Palm Beach, Florida"; the profile carries
"140 Royal Palm Way, Suite 204, Palm Beach, Florida 33480" plus the branch name
and a direct phone. Street and ZIP are worth one request per advisor, so the
enrich stage fetches every profile. Discovery is cached to data/interim so a
re-run does not repeat it.

THE PAGER IS OFF BY ONE, AND SILENTLY
-------------------------------------
Paging is an ASP.NET form POST carrying PageNumber plus a btnNextPage submit,
and the button INCREMENTS what you send. POST PageNumber=1 returns page 2 of 6.
To read page N you post N-1, and page 1 needs PageNumber=0. Posting 1..6 for a
six-page state silently skips page 1 and returns an empty page 7 -- 25 advisors
lost per state, with nothing in the response to say so. The page label
("Page X of Y") is parsed on every request and checked against what was asked
for, so the day that changes it fails loudly.

GET does not page at all: /fa/search?state=fl&PageNumber=3 serves page 1.

NO EMAIL, DELIBERATELY
----------------------
Stifel publishes no address anywhere -- not mailto:, not Cloudflare obfuscation,
not a data attribute; I checked all three after Mariner turned out to be hiding
889 behind Cloudflare. The "Contact" button posts to /ContactFA, a relay form
behind reCAPTCHA. So Stifel is a phone-and-address source: no click-to-email.

Run:  python src/stifel_async.py [--states fl,ga] [--dry-run] [--refresh]
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

BASE = "https://www.stifel.com"
SEARCH = BASE + "/fa/search?state={}"
STIFEL_CRD = "793"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
HEADERS = {"user-agent": UA, "accept-language": "en-US,en;q=0.9",
           "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
PAUSE = 0.4
RETRIES = 3
MAX_PAGES = 200

STATES = [
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "dc", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo",
    "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa",
    "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy",
]

LINK_RE = re.compile(r'search-results-fa-link" href="(/fa/[^"?]+)')
LABEL_RE = re.compile(r"Page (\d+) of (\d+)")
NAME_RE = re.compile(r'<span class="fa-landing-name">(.*?)</span>\s*<br\s*/?>\s*(.*?)\s*</p>', re.S)
PHONE_BLOCK_RE = re.compile(r"<dt>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", re.S)
ADDRESS_RE = re.compile(r'fa-landing-address">(.*?)</div>', re.S)
BRANCH_RE = re.compile(r'<a href="(/branch/[^"]+)">(.*?)</a>', re.S)
DD_RE = re.compile(r"<dd[^>]*>(.*?)</dd>", re.S)
FALINK_RE = re.compile(r'<a href="([^"]+)" class="faLink[^"]*"[^>]*>(.*?)</a>', re.S)
CITY_RE = re.compile(r"^(.*),\s*([A-Za-z .]+?)\s+(\d{5})(?:-\d{4})?$")
TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")

STATE_NAMES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}


def text(fragment: str) -> str:
    return WS.sub(" ", htmllib.unescape(TAGS.sub(" ", fragment or ""))).strip()


def request(session, method, url, **kw):
    """Returns the response, or None on failure, or "gone" on a 404.

    A 404 is a fact about Stifel's data, not a transport problem: their search
    results include advisors whose profile page has been removed. Retrying it
    three times wastes requests and files a stale listing under the same heading
    as a network error."""
    for attempt in range(RETRIES):
        try:
            r = session.request(method, url, headers=HEADERS, timeout=90, **kw)
            if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                return r
            if r.status_code == 404:
                return "gone"
            print(f"    [-] attempt {attempt + 1}: HTTP {r.status_code} {url}")
        except Exception as exc:
            print(f"    [-] attempt {attempt + 1}: {type(exc).__name__} {exc}")
        time.sleep(PAUSE * (2 ** attempt) + random.random())
    return None


def discover_state(session, code, failures):
    """Every profile URL in one state.

    PageNumber is what you POST, not what you get: the btnNextPage submit adds
    one. So page N is requested as N-1, starting from 0."""
    url = SEARCH.format(code)
    found, page, total = [], 0, 1
    while page < total and page < MAX_PAGES:
        r = request(session, "POST", url, data={
            "PageNumber": page, "LastName": "", "State": code,
            "Zipcode": "", "Distance": "", "btnNextPage": "Next Page"})
        if r is None:
            failures.append(f"{code} p{page + 1}")
            return found
        label = LABEL_RE.search(r.text)
        if label:
            shown, total = int(label.group(1)), int(label.group(2))
            if shown != page + 1:
                # the off-by-one changed, or the pager reset -- do not guess
                print(f"    [!] {code}: asked for page {page + 1}, got {shown}")
        found += LINK_RE.findall(r.text)
        page += 1
        time.sleep(PAUSE)
    return found


def parse_profile(markup: str, url: str) -> dict:
    row = {"name": "", "title": "", "phone": "", "toll_free": "", "branch": "",
           "branch_url": "", "address1": "", "address2": "", "city": "",
           "state": "", "postal": "", "website": "", "linkedin": "",
           "profile_url": BASE + url, "slug": url.rsplit("/", 1)[-1]}

    head = NAME_RE.search(markup)
    if head:
        row["name"] = text(head.group(1))
        row["title"] = text(head.group(2))

    for dt, dd in PHONE_BLOCK_RE.findall(markup):
        key, value = text(dt).rstrip(":").lower(), text(dd)
        if key == "phone" and not row["phone"]:
            row["phone"] = value
        elif key.startswith("toll") and not row["toll_free"]:
            row["toll_free"] = value

    block = ADDRESS_RE.search(markup)
    if block:
        chunk = block.group(1)
        branch = BRANCH_RE.search(chunk)
        if branch:
            row["branch_url"] = BASE + branch.group(1)
            row["branch"] = text(branch.group(2))
        lines = [text(d) for d in DD_RE.findall(chunk)]
        lines = [l for l in lines if l and l != row["branch"]
                 and not l.lower().startswith("get directions")]
        # last line is "City, State ZIP"; anything before it is street
        if lines:
            hit = CITY_RE.match(lines[-1])
            if hit:
                row["city"] = hit.group(1).strip()
                name = hit.group(2).strip()
                row["state"] = STATE_NAMES.get(name, name)
                row["postal"] = hit.group(3)
                lines = lines[:-1]
            row["address1"] = lines[0] if lines else ""
            row["address2"] = " ".join(lines[1:]) if len(lines) > 1 else ""

    for href, label in FALINK_RE.findall(markup):
        tag = text(label).lower()
        if tag == "linkedin":
            row["linkedin"] = href
        elif tag == "website" and not row["website"]:
            row["website"] = href
    return row


COLUMNS = ["name", "title", "phone", "toll_free", "address1", "address2", "city",
           "state", "postal", "branch", "branch_url", "website", "linkedin",
           "profile_url", "slug", "team_name", "team_id", "team_kind"]


def team_from_website(url: str):
    """(name, id, kind) for the practice a Stifel advisor's website stands for.

    Stifel's `website` is the practice's own site, not a personal page: 798
    advisors share 290 of them, up to 15 to a site. Nothing else in this feed
    groups people -- there is no team field on the profile page and the branch
    page lists names without saying who works with whom -- so the URL is the
    only practice key available.

    The NAME is the bare domain, deliberately. The obvious move is to split
    adamswealthadvisorygroup.com into "Adams Wealth Advisory Group", and it
    works often enough to be tempting -- but only 59% of these domains split
    into recognisable words, and the rest come out as "Aegeanwealthmanagement"
    or "Alderbridgewmg". A label that is wrong two times in five is worse than
    one that is merely plain, and inventing a practice name is the exact fault
    this project spent the day removing from Merrill and Ameriprise.

    Sites named stifel<city> are the BRANCH's site rather than a practice, so
    they are grouped just the same but labelled for what they are.
    """
    host = re.sub(r"^https?://", "", str(url or "").strip().lower()).split("/")[0]
    host = re.sub(r"^www\.", "", host).strip("/")
    if not host or "." not in host:
        return "", "", ""
    stem = re.sub(r"\.(com|net|org|biz|us|info)$", "", host)
    kind = "branch" if stem.replace("-", "").startswith("stifel") else "practice"
    return host, host, kind


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--states", help="comma-separated codes, for a partial run")
    ap.add_argument("--dry-run", action="store_true", help="discovery only, write nothing")
    ap.add_argument("--refresh", action="store_true", help="ignore the cached discovery")
    args = ap.parse_args()

    codes = args.states.split(",") if args.states else STATES
    session = requests.Session()
    started = time.time()
    failures: list = []

    cache = scratch_path("stifel", "discovery", ext="json")
    if cache.exists() and not args.refresh and not args.states:
        urls = json.loads(cache.read_text(encoding="utf-8"))
        print(f"[*] {len(urls):,} profile URLs from cache ({cache.name}); "
              f"--refresh to re-discover")
    else:
        seen: dict = {}
        for i, code in enumerate(codes, 1):
            hits = discover_state(session, code, failures)
            new = sum(1 for u in hits if u not in seen)
            for u in hits:
                seen.setdefault(u, code)
            print(f"[{i}/{len(codes)}] {code.upper():<3} {len(hits):>4} listed, "
                  f"{new:>4} new  |  total {len(seen):,}")
        urls = sorted(seen)
        if not args.states:
            cache.write_text(json.dumps(urls, indent=1), encoding="utf-8")

    if not urls:
        raise SystemExit("no profile URLs found -- refusing to write an empty roster")
    print(f"[*] discovery: {len(urls):,} advisors in {time.time() - started:.0f}s")

    if args.dry_run:
        print("[*] dry run: profiles not fetched, nothing written")
        return

    rows, gone = [], []
    for i, url in enumerate(urls, 1):
        r = request(session, "GET", BASE + url)
        if r == "gone":
            gone.append(url)
            continue
        if r is None:
            failures.append(url)
            continue
        rows.append(parse_profile(r.text, url))
        if i % 200 == 0:
            print(f"    enriched {i:,}/{len(urls):,}")
        time.sleep(PAUSE)

    for row in rows:
        row["team_name"], row["team_id"], row["team_kind"] = team_from_website(
            row.get("website"))

    df = pd.DataFrame(rows, columns=COLUMNS)
    scratch_path("stifel", "profiles", ext="json").write_text(
        json.dumps(rows, indent=1), encoding="utf-8")
    out = roster_path("stifel")
    df.to_csv(out, index=False)

    print(f"\n[*] {len(df):,} advisors in {time.time() - started:.0f}s -> {out}")
    print(f"    {int((df['phone'] != '').sum()):,} have a direct phone; "
          f"{int((df['address1'] != '').sum()):,} a street address; "
          f"{int((df['postal'] != '').sum()):,} a ZIP")
    print(f"    {int((df['website'] != '').sum()):,} a practice website; "
          f"{int((df['linkedin'] != '').sum()):,} a LinkedIn profile")
    print(f"    NO EMAIL published by Stifel -- checked mailto:, Cloudflare "
          f"obfuscation and data attributes; Contact is a relay form")
    print(f"    {df['state'].replace('', pd.NA).nunique()} states, "
          f"{df['branch'].replace('', pd.NA).nunique()} branches")

    if gone:
        print(f"    {len(gone)} listed advisor(s) have a 404 profile -- stale "
              f"entries in Stifel's own results, not scrape failures: {gone[:5]}")

    blank = int((df["name"] == "").sum())
    if blank:
        print(f"    [!] {blank} profile(s) parsed with no name -- check the markup")
    dupes = len(df) - df["slug"].nunique()
    if dupes:
        print(f"    [!] {dupes} duplicate slug(s)")

    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == STIFEL_CRD]["advisor_crd"].nunique()
        print(f"    SEC lists {sec:,} IARs at CRD {STIFEL_CRD}; the finder "
              f"publishes {len(df):,} ({len(df) / sec:.0%})")
    if failures:
        print(f"    {len(failures)} request(s) FAILED after {RETRIES} attempts: "
              f"{failures[:6]}")


if __name__ == "__main__":
    main()
