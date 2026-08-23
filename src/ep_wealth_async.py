"""EP Wealth Advisors roster -> data/raw/firm_rosters/ep_wealth_<date>.csv

    discovery  GET /sitemap.xml   -> every /our-team/<office>/<person> URL
    enrich     GET <profile>      -> JSON-LD Person block + the office panel

WHY THE SITEMAP
---------------
/our-team renders only nine profiles server-side; the rest arrive by script. The
sitemap is the authoritative list and needs one request: 1,071 URLs, of which
472 are team profiles across 69 offices. No paging, no tokens, no browser.

EVERY PHONE IS AN OFFICE LINE
-----------------------------
Measured, not assumed. Six different people in the Torrance office -- the
co-founder, a wealth advisor, two client relationship staff, the director of
portfolio management and a financial planner -- all publish (310) 543-4559. The
number changes per office (Augusta 207, Portsmouth 603, Newburyport 978, San
Francisco 415) but never per person. So the column is `office_phone`; there is
no `phone`, and the run counts occupants per number to prove it rather than
asserting it.

THE OTHER NUMBER ON THE PAGE IS A FAX
-------------------------------------
Each office panel reads:

    Phone: <a href="tel:(800)%20728-0670">(800) 728-0670</a>
    Fax: (404) 759-2466

A plain phone-shaped regex over the page picks up BOTH and would have filed a
fax as a second contact number -- on the Atlanta profile it is the only
area-code-local number, so it looks more like a direct line than the real
phone does. Only the tel: href is read as a phone; the fax is captured
separately and labelled.

NO EMAIL
--------
Checked six ways: mailto:, Cloudflare email-protection, data-cfemail, &#64; and
\\u0040 entities, [at]/(at) text tricks, and base64. All zero. The only LinkedIn
is the company account.

Run:  python src/ep_wealth_async.py [--dry-run] [--refresh]
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
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path, scratch_path  # one naming convention, defined once

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRANCHES = ROOT / "data" / "output" / "advisor_branches.parquet"

BASE = "https://www.epwealth.com"
SITEMAP = BASE + "/sitemap.xml"
EP_CRD = "111147"
HEADERS = {
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
}
PAUSE = 0.35
RETRIES = 3
PROFILE_MARKER = "advisor-bio"
TOLLFREE = {"800", "833", "844", "855", "866", "877", "888"}

LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
# Trailing slash is optional: most person URLs have none, but a few do
# (berkeley/jonathan-deyoe/). Office index pages -- /our-team/atlanta/ --
# and sub-office pages -- /our-team/brighton/ann-arbor/ -- match this shape
# too and CANNOT be told apart by URL. They are dropped later, by whether
# the page actually carries a JSON-LD Person block.
TEAM_RE = re.compile(r"^https://www\.epwealth\.com/our-team/([a-z0-9\-]+)/([a-z0-9\-]+)/?$")
LD_RE = re.compile(r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>', re.S)
TEL_RE = re.compile(r'href="tel:([^"]+)"')
FAX_RE = re.compile(r"Fax:\s*([\(\)\d\s\-\.]{7,})")
OFFICEHEAD_RE = re.compile(r"<h3[^>]*>\s*([^<]*?Office)\s*</h3>", re.S)
ADDR_RE = re.compile(r'advisor-bio__section__content">\s*<p>\s*(.*?)</p>', re.S)
CITYZIP_RE = re.compile(r"^(.*),\s*([A-Za-z .]+?)\s+(\d{5})(?:-\d{4})?$")
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


def tidy_phone(raw: str) -> str:
    """tel: hrefs arrive percent-encoded, e.g. '(310)%20543-4559'."""
    return WS.sub(" ", urllib.parse.unquote(raw or "")).strip()


def request(session, url, expect: str = ""):
    for attempt in range(RETRIES):
        try:
            r = session.get(url, headers=HEADERS, timeout=90)
            kind = r.headers.get("content-type", "")
            if r.status_code == 200 and ("text/html" in kind or "xml" in kind):
                if not expect or expect in r.text:
                    return r
                print(f"    [-] attempt {attempt + 1}: 200 but no {expect!r} {url}")
            elif r.status_code == 404:
                return "gone"
            else:
                print(f"    [-] attempt {attempt + 1}: HTTP {r.status_code} {url}")
        except Exception as exc:
            print(f"    [-] attempt {attempt + 1}: {type(exc).__name__} {exc}")
        time.sleep(PAUSE * (2 ** attempt) + random.random())
    return None


def discover(session):
    r = request(session, SITEMAP)
    if r is None:
        raise SystemExit("could not fetch the sitemap")
    found = []
    for loc in LOC_RE.findall(r.text):
        m = TEAM_RE.match(loc.strip())
        if m:
            found.append((loc.strip(), m.group(1), m.group(2)))
    offices = {o for _, o, _ in found}
    print(f"[*] sitemap: {len(found):,} team profiles across {len(offices)} offices")
    return sorted(set(found))


COLUMNS = ["name", "title", "office", "office_label", "office_address", "city",
           "state", "postal", "office_phone", "phone_kind", "office_fax",
           "photo", "profile_url", "slug"]


def parse_profile(markup, url, office_slug, slug):
    # The JSON-LD Person block is the cleanest source for name/title/address --
    # it is what the site itself hands to search engines.
    data = {}
    for blob in LD_RE.findall(markup):
        try:
            cand = json.loads(blob)
        except Exception:
            continue
        if cand.get("@type") == "Person":
            data = cand
            break
    addr = data.get("address") or {}

    street = text(addr.get("streetAddress") or "")
    city = text(addr.get("addressLocality") or "")
    state_name = text(addr.get("addressRegion") or "")
    postal = text(addr.get("postalCode") or "")
    state = STATE_NAMES.get(state_name, state_name[:2].upper() if state_name else "")
    # streetAddress repeats the city/state/zip; trim so the column is a street
    if city and city in street:
        street = street.split(city)[0].strip(" ,")

    tel = TEL_RE.search(markup)
    fax = FAX_RE.search(markup)
    head = OFFICEHEAD_RE.search(markup)
    return {
        "name": text(data.get("name") or ""),
        "title": text(data.get("jobTitle") or ""),
        "office": office_slug,
        "office_label": text(head.group(1)) if head else "",
        "office_address": street,
        "city": city,
        "state": state,
        "postal": postal,
        "office_phone": tidy_phone(tel.group(1)) if tel else "",
        "phone_kind": "",                       # filled in after the run
        # captured separately and NEVER merged into the phone column
        "office_fax": text(fax.group(1)) if fax else "",
        "photo": data.get("image") or "",
        "profile_url": url,
        "slug": slug,
    }


def classify(df):
    """Label each number by how many people publish it."""
    counts = Counter(df.loc[df["office_phone"] != "", "office_phone"])

    def label(p):
        if not p:
            return ""
        digits = re.sub(r"\D", "", p)
        if digits[:1] == "1":
            digits = digits[1:]
        if digits[:3] in TOLLFREE:
            return "toll-free"
        n = counts[p]
        return "single-occupant" if n == 1 else ("shared" if n <= 5 else "switchboard")
    return df["office_phone"].map(label), counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="discovery only")
    ap.add_argument("--refresh", action="store_true", help="ignore the cached discovery")
    ap.add_argument("--limit", type=int, help="only the first N profiles, for a trial")
    args = ap.parse_args()

    started = time.time()
    session = requests.Session()
    failures, gone, not_pages = [], [], []

    cache = scratch_path("ep_wealth", "discovery", ext="json")
    if cache.exists() and not args.refresh:
        people = [tuple(x) for x in json.loads(cache.read_text(encoding="utf-8"))]
        print(f"[*] {len(people):,} profiles from cache; --refresh to re-discover")
    else:
        people = discover(session)
        cache.write_text(json.dumps(people, indent=1), encoding="utf-8")

    if not people:
        raise SystemExit("no profiles found -- refusing to write an empty roster")
    if args.dry_run:
        print(f"[*] dry run: would fetch {len(people):,} profiles")
        return
    if args.limit:
        people = people[:args.limit]

    rows = []
    for i, (url, office, slug) in enumerate(people, 1):
        r = request(session, url, expect=PROFILE_MARKER)
        if r == "gone":
            gone.append(slug)
            continue
        if r is None:
            # Resolves but has no advisor-bio: a sub-office landing page
            # ("Financial Advisors Serving Ann Arbor, Michigan"), not an
            # advisor we failed to fetch. Filing it as a failure implies
            # data was lost when none was.
            plain = request(session, url)
            (not_pages if plain not in (None, "gone") else failures).append(slug)
            continue
        rows.append(parse_profile(r.text, url, office, slug))
        if i % 100 == 0:
            print(f"    enriched {i:,}/{len(people):,}")
        time.sleep(PAUSE)

    # A URL that looked like a person but has no Person schema is an office
    # or sub-office index page, not a missed advisor.
    not_people = [r for r in rows if not r["name"]]
    blanks = not_people
    if blanks:
        print(f"[*] repairing {len(blanks)} blank profile(s)")
        for row in blanks:
            time.sleep(PAUSE * 3)
            rr = request(session, row["profile_url"], expect=PROFILE_MARKER)
            if rr not in (None, "gone"):
                row.update(parse_profile(rr.text, row["profile_url"],
                                         row["office"], row["slug"]))
        still = sum(1 for r in rows if not r["name"])
        print(f"    {len(blanks) - still} recovered, {still} carry no Person "
              f"schema -- office/sub-office index pages, dropped")
    rows = [r for r in rows if r["name"]]

    df = pd.DataFrame(rows, columns=COLUMNS)
    df["phone_kind"], counts = classify(df)
    scratch_path("ep_wealth", "profiles", ext="json").write_text(
        json.dumps(rows, indent=1), encoding="utf-8")
    out = roster_path("ep_wealth")
    df.to_csv(out, index=False)

    print(f"\n[*] {len(df):,} people in {time.time() - started:.0f}s -> {out}")
    print(f"    NO EMAIL published by EP Wealth -- checked mailto:, Cloudflare, "
          f"data-cfemail, entity escapes, [at] tricks and base64")
    print(f"    {int((df['office_phone'] != '').sum()):,} have an OFFICE phone "
          f"({df.loc[df['office_phone'] != '', 'office_phone'].nunique()} distinct "
          f"numbers across {df['office'].nunique()} offices)")
    print(f"    {int((df['office_fax'] != '').sum()):,} also publish a FAX -- kept in "
          f"its own column so it can never be dialled as a phone")
    print(f"    {int((df['office_address'] != '').sum()):,} an office address; "
          f"{int((df['postal'] != '').sum()):,} a ZIP; "
          f"{df['state'].replace('', pd.NA).nunique()} states")
    print("\n    phone_kind: " + ", ".join(
        f"{k} {v:,}" for k, v in df["phone_kind"].value_counts().items() if k))
    print("    most-shared: " + ", ".join(f"{p} x{n}" for p, n in counts.most_common(5)))

    # A slug can legitimately appear twice: 13 advisors are listed at two
    # offices, with a different office phone at each. profile_url is the
    # unique key, not slug.
    multi = len(df) - df["slug"].nunique()
    if multi:
        print(f"    {multi} row(s) are advisors listed at a SECOND office "
              f"({df['slug'].nunique():,} distinct people, {len(df):,} "
              f"person-office rows)")
    url_dupes = len(df) - df["profile_url"].nunique()
    if url_dupes:
        print(f"    [!] {url_dupes} duplicate profile_url(s) -- that WOULD be a bug")
    if not_pages:
        print(f"    {len(not_pages)} sitemap URL(s) are sub-office landing pages, "
              f"not advisors: {not_pages[:5]}")
    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == EP_CRD]["advisor_crd"].nunique()
        print(f"    SEC lists {sec:,} IARs at CRD {EP_CRD}; this site lists "
              f"{len(df):,} people of all roles ({len(df) / sec:.0%})")
    if gone:
        print(f"    {len(gone)} profile(s) 404: {gone[:4]}")
    if failures:
        print(f"    {len(failures)} FAILED: {failures[:5]}")


if __name__ == "__main__":
    main()
