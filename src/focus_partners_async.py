"""Focus Partners advisor roster -> data/raw/firm_rosters/focus_partners_<date>.csv

Two stages:

  discovery  GET /advisors?types=562&offset=<n>   (15 per page, n = 0,15,30...)
  enrich     GET /people/<slug>

THE "LOAD MORE RESULTS" BUTTON IS A RED HERRING
-----------------------------------------------
The site is Craft CMS with Sprig/htmx, and the button posts to

    /index.php/actions/sprig-core/components/render?types=562&offset=15

which returns HTTP 400 without a signed `sprig:config` token. Reproducing that
handshake is unnecessary: the ordinary listing page accepts `offset` directly
and returns a clean, non-overlapping slice.

    offset 0  -> 15 advisors, first is will-aaron
    offset 15 -> 15 advisors, first is eric-anthony, zero overlap with offset 0
    offset 30 -> 15 advisors, first is jeff-barnett, zero overlap with offset 15

`limit` is ignored -- 15 is fixed. The end was found by bisection rather than
assumed: offset 795 returns 3 and offset 810 returns 0, so 798 advisors.

WHAT IS ON A PROFILE
--------------------
Name, designations, job title, work EMAIL (a plain mailto:, no obfuscation),
direct phone, and a full street address with city, state and ZIP. No lat/lon,
so placement goes through the ZIP. The only LinkedIn on the page is the company
account, not the individual's.

Run:  python src/focus_partners_async.py [--dry-run] [--refresh]
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

BASE = "https://www.focuspartners.com"
LISTING = BASE + "/advisors"
ADVISOR_TYPE = "562"                 # the "advisor" people-type filter
FOCUS_CRD = "159289"
HEADERS = {
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
}
PAGE_SIZE = 15
PAUSE = 0.4
RETRIES = 3
MAX_OFFSET = 5000
PROFILE_MARKER = "headline-2"        # every real profile heading carries it

SLUG_RE = re.compile(r'href="https://www\.focuspartners\.com/people/([a-z0-9\-]+)"')
NAME_RE = re.compile(r'<h1 class="headline-2[^"]*">(.*?)</h1>', re.S)
LOCTAG_RE = re.compile(r'<p class="xl:text-16 uppercase">(.*?)</p>', re.S)
DESIG_RE = re.compile(r'<p class="text-blue font-bold">(.*?)</p>', re.S)
TITLE_RE = re.compile(r'<p class="text-16 md:text-20 xl:text-22 uppercase">(.*?)</p>', re.S)
ADDR_RE = re.compile(r'<p>([^<]*(?:<br\s*/?>[^<]*)+)</p>', re.S)
MAIL_RE = re.compile(r'href="mailto:([^"?]+)"')
TEL_RE = re.compile(r'href="tel:([^"]+)"')
CITY_RE = re.compile(r"^(.*),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?$")
TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def text(fragment: str) -> str:
    return WS.sub(" ", htmllib.unescape(TAGS.sub("", fragment or ""))).strip()


def request(session, url, params=None, expect: str = ""):
    """Fetch with retries. `expect` is a marker the real page must contain --
    a 200 carrying a shell or error page would otherwise parse to an empty row
    and be indistinguishable from an advisor with no details."""
    for attempt in range(RETRIES):
        try:
            r = session.get(url, headers=HEADERS, params=params, timeout=90)
            if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
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


def discover(session, failures):
    """Walk the offset until a page comes back empty."""
    slugs, offset = [], 0
    while offset < MAX_OFFSET:
        r = request(session, LISTING, params={"types": ADVISOR_TYPE, "offset": offset})
        if r is None:
            failures.append(f"listing offset {offset}")
            break
        found = SLUG_RE.findall(r.text)
        if not found:
            break
        slugs += found
        print(f"[offset {offset:>4}] {len(found):>3} advisors  |  total {len(slugs):,}")
        offset += PAGE_SIZE
        time.sleep(PAUSE)
    return slugs


COLUMNS = ["name", "title", "designations", "email", "phone", "address1", "city",
           "state", "postal", "location_tag", "profile_url", "slug"]


def parse_profile(markup: str, slug: str) -> dict:
    name = NAME_RE.search(markup)
    loc = LOCTAG_RE.search(markup)
    desig = DESIG_RE.search(markup)
    title = TITLE_RE.search(markup)
    mail = MAIL_RE.search(markup)
    tel = TEL_RE.search(markup)

    street = city = state = postal = ""
    for block in ADDR_RE.findall(markup):
        lines = [text(l) for l in re.split(r"<br\s*/?>", block)]
        lines = [l for l in lines if l]
        if len(lines) >= 2:
            hit = CITY_RE.match(lines[-1])
            if hit:                       # only accept a block that really ends
                street = " ".join(lines[:-1])   # in "City, ST ZIP"
                city, state, postal = hit.group(1).strip(), hit.group(2), hit.group(3)
                break
    return {
        "name": text(name.group(1)) if name else "",
        "title": text(title.group(1)) if title else "",
        "designations": text(desig.group(1)) if desig else "",
        "email": mail.group(1).strip() if mail else "",
        "phone": tel.group(1).strip() if tel else "",
        "address1": street,
        "city": city,
        "state": state,
        "postal": postal,
        # the small caps line above the name, e.g. "Livingston, NJ"
        "location_tag": text(loc.group(1)) if loc else "",
        "profile_url": f"{BASE}/people/{slug}",
        "slug": slug,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="discovery only")
    ap.add_argument("--refresh", action="store_true", help="ignore the cached discovery")
    ap.add_argument("--limit", type=int, help="only the first N profiles, for a trial")
    args = ap.parse_args()

    started = time.time()
    session = requests.Session()
    failures, gone = [], []

    cache = scratch_path("focus_partners", "discovery", ext="json")
    if cache.exists() and not args.refresh:
        slugs = json.loads(cache.read_text(encoding="utf-8"))
        print(f"[*] {len(slugs):,} slugs from cache; --refresh to re-discover")
    else:
        slugs = discover(session, failures)
        dupes = len(slugs) - len(set(slugs))
        if dupes:
            # offsets should tile without overlap; if they stop doing so the
            # paging model has changed and the totals cannot be trusted
            print(f"    [!] {dupes} duplicate slug(s) across offsets -- paging may "
                  f"have changed")
        slugs = sorted(set(slugs))
        if slugs:
            cache.write_text(json.dumps(slugs, indent=1), encoding="utf-8")

    if not slugs:
        raise SystemExit("no advisors found -- refusing to write an empty roster")
    print(f"[*] discovery: {len(slugs):,} advisors in {time.time() - started:.0f}s")
    if args.dry_run:
        return
    if args.limit:
        slugs = slugs[:args.limit]

    rows = []
    for i, slug in enumerate(slugs, 1):
        r = request(session, f"{BASE}/people/{slug}", expect=PROFILE_MARKER)
        if r == "gone":
            gone.append(slug)
            continue
        if r is None:
            failures.append(slug)
            continue
        rows.append(parse_profile(r.text, slug))
        if i % 100 == 0:
            print(f"    enriched {i:,}/{len(slugs):,}")
        time.sleep(PAUSE)

    # Second pass over anything that parsed without a name -- throttling is
    # bursty and a URL that failed mid-run usually succeeds a minute later.
    blanks = [r for r in rows if not r["name"]]
    if blanks:
        print(f"[*] repairing {len(blanks)} blank profile(s)")
        for row in blanks:
            time.sleep(PAUSE * 3)
            rr = request(session, row["profile_url"], expect=PROFILE_MARKER)
            if rr not in (None, "gone"):
                row.update(parse_profile(rr.text, row["slug"]))
        still = sum(1 for r in rows if not r["name"])
        print(f"    {len(blanks) - still} recovered, {still} still blank")

    df = pd.DataFrame(rows, columns=COLUMNS)
    scratch_path("focus_partners", "profiles", ext="json").write_text(
        json.dumps(rows, indent=1), encoding="utf-8")
    out = roster_path("focus_partners")
    df.to_csv(out, index=False)

    emails = int((df["email"] != "").sum())
    print(f"\n[*] {len(df):,} advisors in {time.time() - started:.0f}s -> {out}")
    print(f"    {emails:,} have an email ({emails / len(df):.1%}); "
          f"{int((df['phone'] != '').sum()):,} a phone")
    print(f"    {int((df['address1'] != '').sum()):,} a street address; "
          f"{int((df['postal'] != '').sum()):,} a ZIP; no coordinates in this feed")
    print(f"    {df['state'].replace('', pd.NA).nunique()} states, "
          f"{int((df['designations'] != '').sum()):,} carry designations")

    blank = int((df["name"] == "").sum())
    if blank:
        print(f"    [!] {blank} profile(s) parsed with no name -- check the markup")
    dupes = len(df) - df["slug"].nunique()
    if dupes:
        print(f"    [!] {dupes} duplicate slug(s)")
    if gone:
        print(f"    {len(gone)} listed profile(s) 404: {gone[:4]}")

    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == FOCUS_CRD]["advisor_crd"].nunique()
        print(f"    SEC lists {sec:,} IARs at CRD {FOCUS_CRD}; the directory "
              f"publishes {len(df):,} ({len(df) / sec:.0%})")
    if failures:
        print(f"    {len(failures)} request(s) FAILED after {RETRIES} attempts: "
              f"{failures[:5]}")


if __name__ == "__main__":
    main()
