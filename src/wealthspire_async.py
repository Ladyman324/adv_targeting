"""Wealthspire Advisors roster -> data/raw/firm_rosters/wealthspire_<date>.csv

    discovery  GET /sitemap.xml    -> 382 /our-team/<slug>/ profiles
    enrich     GET <profile>       -> name, title, email, phone, bio

The site is Umbraco, and /our-team/ renders no profiles server-side -- there are
zero team links in its HTML and no XHR endpoint behind it. The sitemap is the
whole list in one request, so nothing needs a browser.

PHONE: MOSTLY DIRECT, AND THE EXTENSION IS THE TELL
---------------------------------------------------
Unlike Captrust or EP Wealth, the tel: link here is labelled per person
("Phone for Adam Corder") and the numbers differ between colleagues. Two forms
appear:

    410.988.9494 ext. 99001    a shared main number plus a personal extension
    212.301.1165               a bare number

Both are split out: `phone_base`, `phone_ext`, and a `phone_kind` computed from
how many colleagues share the base. A shared base WITH a personal extension
still reaches one person, so it is labelled `extension`, not `switchboard` --
that distinction would be lost if the extension were dropped or the string
compared whole.

NO OFFICE FIELD, DELIBERATELY
-----------------------------
Profiles carry no structured location. One bio happened to say "the firm's
Annapolis, Maryland office", so I tested a pattern for that phrase across a
twelve-profile sample: it matched 0 of 12. Rather than ship a column that is
empty ~95% of the time, there is none -- the phone AREA CODE is the only
geography, as with RBC.

Run:  python src/wealthspire_async.py [--dry-run] [--refresh] [--limit N]
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
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path, scratch_path  # one naming convention, defined once

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRANCHES = ROOT / "data" / "output" / "advisor_branches.parquet"

BASE = "https://www.wealthspire.com"
SITEMAP = BASE + "/sitemap.xml"
WEALTHSPIRE_CRD = "106181"
HEADERS = {
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
}
PAUSE = 0.35
RETRIES = 3
PROFILE_MARKER = "person-content"
TOLLFREE = {"800", "833", "844", "855", "866", "877", "888"}

LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
TEAM_RE = re.compile(r"^https://www\.wealthspire\.com/our-team/([^/]+)/$")
H1_RE = re.compile(r'<h1[^>]*>(.*?)</h1>', re.S)
TITLE_RE = re.compile(r'<span class="display-5[^"]*">(.*?)</span>', re.S)
MAIL_RE = re.compile(r'href="mailto:([^"?]+)"')
TEL_RE = re.compile(r'href="tel:([^"]+)"')
BIO_RE = re.compile(r'<div class="col-12 col-md-8 order-md-1[^"]*">(.*?)</div>', re.S)
EXT_RE = re.compile(r"^(.*?)\s*(?:ext\.?|x)\s*([\d]+)\s*$", re.I)
TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def text(fragment: str) -> str:
    return WS.sub(" ", htmllib.unescape(TAGS.sub(" ", fragment or ""))).strip()


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


def split_phone(raw: str):
    """'410.988.9494 ext. 99001' -> ('410.988.9494', '99001')."""
    value = WS.sub(" ", htmllib.unescape(raw or "")).strip()
    hit = EXT_RE.match(value)
    if hit:
        return hit.group(1).strip(), hit.group(2).strip()
    return value, ""


COLUMNS = ["name", "title", "email", "phone", "phone_base", "phone_ext",
           "phone_kind", "area_code", "bio", "profile_url", "slug"]


def parse_profile(markup, url, slug):
    h1 = H1_RE.search(markup)
    title = TITLE_RE.search(markup)
    mail = MAIL_RE.search(markup)
    tel = TEL_RE.search(markup)
    bio = BIO_RE.search(markup)
    raw = tel.group(1) if tel else ""
    base, ext = split_phone(raw)
    digits = re.sub(r"\D", "", base)
    if digits[:1] == "1":
        digits = digits[1:]
    return {
        "name": text(h1.group(1)) if h1 else "",
        "title": text(title.group(1)) if title else "",
        "email": mail.group(1).strip() if mail else "",
        "phone": WS.sub(" ", htmllib.unescape(raw)).strip(),
        "phone_base": base,
        "phone_ext": ext,
        "phone_kind": "",                    # filled in after the run
        "area_code": digits[:3] if len(digits) >= 10 else "",
        "bio": text(bio.group(1))[:2000] if bio else "",
        "profile_url": url,
        "slug": slug,
    }


def classify(df):
    """Label by how many colleagues share the BASE number.

    A shared base plus a personal extension still reaches one person, so it is
    called `extension` rather than `switchboard` -- comparing the whole string
    would call every extension unique, and comparing only the base would call
    every extension a switchboard. Both would be wrong."""
    counts = Counter(df.loc[df["phone_base"] != "", "phone_base"])

    def label(row):
        base = row["phone_base"]
        if not base:
            return ""
        digits = re.sub(r"\D", "", base)
        if digits[:1] == "1":
            digits = digits[1:]
        if digits[:3] in TOLLFREE:
            return "toll-free"
        if row["phone_ext"]:
            return "extension"
        n = counts[base]
        return "direct" if n == 1 else ("shared" if n <= 5 else "switchboard")
    return df.apply(label, axis=1), counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="discovery only")
    ap.add_argument("--refresh", action="store_true", help="ignore the cached discovery")
    ap.add_argument("--limit", type=int, help="only the first N profiles, for a trial")
    args = ap.parse_args()

    started = time.time()
    session = requests.Session()
    failures, gone = [], []

    cache = scratch_path("wealthspire", "discovery", ext="json")
    if cache.exists() and not args.refresh:
        people = [tuple(x) for x in json.loads(cache.read_text(encoding="utf-8"))]
        print(f"[*] {len(people):,} profiles from cache; --refresh to re-discover")
    else:
        r = request(session, SITEMAP)
        if r is None:
            raise SystemExit("could not fetch the sitemap")
        people = sorted({(loc.strip(), TEAM_RE.match(loc.strip()).group(1))
                         for loc in LOC_RE.findall(r.text)
                         if TEAM_RE.match(loc.strip())})
        print(f"[*] sitemap: {len(people):,} team profiles")
        cache.write_text(json.dumps(people, indent=1), encoding="utf-8")

    if not people:
        raise SystemExit("no profiles found -- refusing to write an empty roster")
    if args.dry_run:
        print(f"[*] dry run: would fetch {len(people):,} profiles")
        return
    if args.limit:
        people = people[:args.limit]

    rows = []
    for i, (url, slug) in enumerate(people, 1):
        r = request(session, url, expect=PROFILE_MARKER)
        if r == "gone":
            gone.append(slug)
            continue
        if r is None:
            failures.append(slug)
            continue
        rows.append(parse_profile(r.text, url, slug))
        if i % 100 == 0:
            print(f"    enriched {i:,}/{len(people):,}")
        time.sleep(PAUSE)

    blanks = [r for r in rows if not r["name"]]
    if blanks:
        print(f"[*] repairing {len(blanks)} blank profile(s)")
        for row in blanks:
            time.sleep(PAUSE * 3)
            rr = request(session, row["profile_url"], expect=PROFILE_MARKER)
            if rr not in (None, "gone"):
                row.update(parse_profile(rr.text, row["profile_url"], row["slug"]))
        still = sum(1 for r in rows if not r["name"])
        print(f"    {len(blanks) - still} recovered, {still} still blank")

    df = pd.DataFrame(rows, columns=COLUMNS)
    df["phone_kind"], counts = classify(df)
    scratch_path("wealthspire", "profiles", ext="json").write_text(
        json.dumps(rows, indent=1), encoding="utf-8")
    out = roster_path("wealthspire")
    df.to_csv(out, index=False)

    emails = int((df["email"] != "").sum())
    phones = int((df["phone"] != "").sum())
    print(f"\n[*] {len(df):,} people in {time.time() - started:.0f}s -> {out}")
    print(f"    {emails:,} have a personal EMAIL ({emails / len(df):.1%}); "
          f"{phones:,} a phone ({int((df['phone_ext'] != '').sum()):,} with an extension)")
    print(f"    {df.loc[df['phone_base'] != '', 'phone_base'].nunique()} distinct base "
          f"numbers, {df['area_code'].replace('', pd.NA).nunique()} area codes")
    print(f"    NO office/address field on these profiles -- area code is the only "
          f"geography")
    print("\n    phone_kind: " + ", ".join(
        f"{k} {v:,}" for k, v in df["phone_kind"].value_counts().items() if k))
    print("    most-shared base numbers: " +
          ", ".join(f"{p} x{n}" for p, n in counts.most_common(4)))

    blank = int((df["name"] == "").sum())
    if blank:
        print(f"    [!] {blank} profile(s) with no name")
    dupes = len(df) - df["slug"].nunique()
    if dupes:
        print(f"    [!] {dupes} duplicate slug(s)")
    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == WEALTHSPIRE_CRD]["advisor_crd"].nunique()
        print(f"    SEC lists {sec:,} IARs at CRD {WEALTHSPIRE_CRD}; this site lists "
              f"{len(df):,} people of all roles ({len(df) / sec:.0%})")
    if gone:
        print(f"    {len(gone)} profile(s) 404: {gone[:4]}")
    if failures:
        print(f"    {len(failures)} FAILED: {failures[:5]}")


if __name__ == "__main__":
    main()
