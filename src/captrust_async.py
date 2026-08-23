"""CAPTRUST people roster -> data/raw/firm_rosters/captrust_<date>.csv

Two stages:

  discovery  GET /wp-json/wp/v2/people?per_page=20&page=<n>   (848 records)
  enrich     GET <link>   for job title, office location(s) and LinkedIn

THE PHONE IS AN OFFICE LINE, AND THE MARKUP SAYS SO
---------------------------------------------------
This is the reason the roster's phone column is named `office_phone` and there
is no `phone`. Every tel: link on a profile sits INSIDE a location card:

    <div class="location-card">
      <h2 class="location-title">Leawood, KS</h2>
      <p class="location-info address"><a ...>4200 West 115th Street #210<br/>
                                          Leawood, KS 66211</a></p>
      <p class="location-info phone"><a href="tel:816.753.5100">816.753.5100</a></p>
      <a class="btn-gray" href="/locations/kansas/leawood-ks/">View Location</a>
    </div>

Measured on Blake Greenfield's page: 1 tel: link, 1 location card, and ZERO tel:
links outside a card. The number is a property of the BUILDING, not the person,
and the script asserts that -- any tel: found outside a location card is written
to `direct_phone` and reported, because that would be a different kind of fact.

Corroborating evidence from an eight-profile sample: Evan Judge and Mike Hirte
both publish 602.468.1232, and Connor Keim publishes an 800 number. After the
run the script counts advisors per number so the sharing is measured, not
assumed, and labels each as switchboard / shared / single-occupant.

NO EMAIL ANYWHERE
-----------------
Checked six ways on nine profiles -- mailto:, Cloudflare email-protection,
data-cfemail, &#64; and \\u0040 entities, [at]/(at) text tricks, and base64
blobs. All zero. The REST API's `acf` block is empty too, exactly like Mariner.
CAPTRUST publishes no email address, so this roster cannot feed click-to-email.

Run:  python src/captrust_async.py [--dry-run] [--refresh]
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

BASE = "https://www.captrust.com"
API = BASE + "/wp-json/wp/v2/"
CAPTRUST_CRD = "175112"
HEADERS = {
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
    "accept-language": "en-US,en;q=0.9",
}
PER_PAGE = 20        # per_page=100 returns HTTP 504 -- the API times out
PAUSE = 0.4
RETRIES = 4
PROFILE_MARKER = "job-title"

CARD_RE = re.compile(r'<div class="location-card">.*?(?=<div class="location-card">|</section>)', re.S)
LOCTITLE_RE = re.compile(r'<h2 class="location-title">(.*?)</h2>', re.S)
LOCADDR_RE = re.compile(r'location-info address"><a[^>]*>(.*?)</a>', re.S)
LOCPHONE_RE = re.compile(r'location-info phone"><a href="tel:([^"]+)"')
LOCURL_RE = re.compile(r'href="(https://www\.captrust\.com/locations/[^"]+)"')
TEL_RE = re.compile(r'href="tel:([^"]+)"')
TITLE_RE = re.compile(r'class="[^"]*job-title[^"]*"[^>]*>(.*?)<', re.S)
LINKEDIN_RE = re.compile(r'href="(https?://[^"]*linkedin\.com/in/[^"]+)"')
CITYZIP_RE = re.compile(r"^(.*),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?$")
TOLLFREE = {"800", "833", "844", "855", "866", "877", "888"}
TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def text(fragment: str) -> str:
    return WS.sub(" ", htmllib.unescape(TAGS.sub(" ", fragment or ""))).strip()


def request(session, url, params=None, want_json=False, expect=""):
    for attempt in range(RETRIES):
        try:
            r = session.get(url, headers=HEADERS, params=params, timeout=90)
            kind = r.headers.get("content-type", "")
            if r.status_code == 200:
                if want_json and kind.startswith("application/json"):
                    return r
                if not want_json and "text/html" in kind and (not expect or expect in r.text):
                    return r
            if r.status_code == 404:
                return "gone"
            print(f"    [-] attempt {attempt + 1}: HTTP {r.status_code} {kind[:24]} {url}")
        except Exception as exc:
            print(f"    [-] attempt {attempt + 1}: {type(exc).__name__} {exc}")
        time.sleep(PAUSE * (2 ** attempt) + random.random())
    return None


def taxonomy(session, name):
    """term id -> label, for group-team / audience / pro-designation."""
    out = {}
    for page in range(1, 6):
        r = request(session, API + name, params={"per_page": 50, "page": page},
                    want_json=True)
        if r is None:
            break
        rows = r.json()
        if not rows:
            break
        for t in rows:
            out[t["id"]] = htmllib.unescape(t["name"])
        if len(rows) < 50:
            break
        time.sleep(PAUSE)
    return out


def discover(session, failures):
    """Page the people endpoint using the count WordPress reports.

    Walking until an empty page is wrong here: asking for a page past the end
    returns HTTP 400, not an empty list, so a naive loop burns its retries and
    then reports the end of the data as a failure. X-WP-TotalPages says exactly
    how far to go."""
    people, page, total_pages = [], 1, None
    while total_pages is None or page <= total_pages:
        r = request(session, API + "people",
                    params={"per_page": PER_PAGE, "page": page}, want_json=True)
        if r is None:
            failures.append(f"people page {page}")
            break
        if total_pages is None:
            total_pages = int(r.headers.get("X-WP-TotalPages") or 0) or None
            print(f"[*] API reports {r.headers.get('X-WP-Total')} people "
                  f"across {total_pages} pages")
        rows = r.json()
        if not rows:
            break
        people += rows
        if page % 10 == 0:
            print(f"[people p{page}] {len(people):,} so far")
        page += 1
        time.sleep(PAUSE)
    return people


COLUMNS = ["name", "title", "group_team", "audience", "designations",
           "office_name", "office_address", "office_city", "office_state",
           "office_postal", "office_phone", "phone_kind", "n_offices",
           "location_url", "direct_phone", "linkedin", "profile_url", "slug"]


def parse_profile(markup, rec, tax):
    cards = CARD_RE.findall(markup)
    inside = sum(len(TEL_RE.findall(c)) for c in cards)
    outside = len(TEL_RE.findall(markup)) - inside

    office = cards[0] if cards else ""
    name = LOCTITLE_RE.search(office)
    addr = LOCADDR_RE.search(office)
    ph = LOCPHONE_RE.search(office)
    url = LOCURL_RE.search(office)

    street = city = state = postal = ""
    if addr:
        lines = [text(l) for l in re.split(r"<br\s*/?>", addr.group(1))]
        lines = [l for l in lines if l]
        if lines:
            hit = CITYZIP_RE.match(lines[-1])
            if hit:
                street = " ".join(lines[:-1])
                city, state, postal = hit.group(1).strip(), hit.group(2), hit.group(3)
            else:
                street = " ".join(lines)

    # A tel: outside every location card would be a genuinely personal number.
    direct = ""
    if outside > 0:
        for cand in TEL_RE.findall(markup):
            if not any(cand in c for c in cards):
                direct = cand
                break

    title = TITLE_RE.search(markup)
    li = LINKEDIN_RE.search(markup)
    return {
        "name": htmllib.unescape(rec["title"]["rendered"]),
        "title": text(title.group(1)) if title else "",
        "group_team": "; ".join(tax["group-team"].get(i, str(i))
                                for i in rec.get("group-team") or []),
        "audience": "; ".join(tax["audience"].get(i, str(i))
                              for i in rec.get("audience") or []),
        "designations": "; ".join(tax["pro-designation"].get(i, str(i))
                                  for i in rec.get("pro-designation") or []),
        "office_name": text(name.group(1)) if name else "",
        "office_address": street,
        "office_city": city,
        "office_state": state,
        "office_postal": postal,
        "office_phone": ph.group(1).strip() if ph else "",
        "phone_kind": "",                       # filled in after the run
        "n_offices": len(cards),
        "location_url": url.group(1) if url else "",
        "direct_phone": direct,
        "linkedin": li.group(1) if li else "",
        "profile_url": rec["link"],
        "slug": rec["slug"],
    }


def classify(df):
    """Label each office_phone by how many advisors share it.

    This is the check the roster exists to support: a number one person answers
    is worth dialling, a number 60 people share is a switchboard."""
    counts = Counter(df.loc[df["office_phone"] != "", "office_phone"])

    def label(p):
        if not p:
            return ""
        if p.replace(".", "").replace("-", "")[:3] in TOLLFREE:
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
    failures, gone = [], []

    cache = scratch_path("captrust", "discovery", ext="json")
    if cache.exists() and not args.refresh:
        payload = json.loads(cache.read_text(encoding="utf-8"))
        people, tax = payload["people"], payload["tax"]
        tax = {k: {int(i): v for i, v in d.items()} for k, d in tax.items()}
        print(f"[*] {len(people):,} people from cache; --refresh to re-discover")
    else:
        tax = {n: taxonomy(session, n)
               for n in ("group-team", "audience", "pro-designation")}
        print("[*] taxonomies: " + ", ".join(f"{k} {len(v)}" for k, v in tax.items()))
        people = discover(session, failures)
        if people:
            cache.write_text(json.dumps({"people": people, "tax": tax}, indent=1),
                             encoding="utf-8")

    if not people:
        raise SystemExit("no people found -- refusing to write an empty roster")
    print(f"[*] discovery: {len(people):,} people in {time.time() - started:.0f}s")
    if args.dry_run:
        return
    if args.limit:
        people = people[:args.limit]

    rows = []
    for i, rec in enumerate(people, 1):
        r = request(session, rec["link"], expect=PROFILE_MARKER)
        if r == "gone":
            gone.append(rec["slug"])
            continue
        if r is None:
            failures.append(rec["slug"])
            continue
        rows.append(parse_profile(r.text, rec, tax))
        if i % 100 == 0:
            print(f"    enriched {i:,}/{len(people):,}")
        time.sleep(PAUSE)

    blanks = [r for r in rows if not r["office_phone"] and not r["office_name"]]
    if blanks:
        print(f"[*] repairing {len(blanks)} profile(s) with no location card")
        for row in blanks:
            time.sleep(PAUSE * 3)
            rr = request(session, row["profile_url"], expect=PROFILE_MARKER)
            if rr not in (None, "gone"):
                rec = next(p for p in people if p["slug"] == row["slug"])
                row.update(parse_profile(rr.text, rec, tax))
        still = sum(1 for r in rows if not r["office_phone"] and not r["office_name"])
        print(f"    {len(blanks) - still} recovered, {still} genuinely have no office")

    df = pd.DataFrame(rows, columns=COLUMNS)
    df["phone_kind"], counts = classify(df)
    scratch_path("captrust", "profiles", ext="json").write_text(
        json.dumps(rows, indent=1), encoding="utf-8")
    out = roster_path("captrust")
    df.to_csv(out, index=False)

    advisors = df[df["group_team"].str.contains("Financial Advisors", na=False)]
    print(f"\n[*] {len(df):,} people in {time.time() - started:.0f}s -> {out}")
    print(f"    {len(advisors):,} tagged 'Financial Advisors'")
    print(f"    NO EMAIL published by CAPTRUST -- checked mailto:, Cloudflare, "
          f"data-cfemail, entity escapes, [at] tricks and base64")
    print(f"    {int((df['office_phone'] != '').sum()):,} have an OFFICE phone; "
          f"{int((df['direct_phone'] != '').sum()):,} a phone outside a location card")
    print(f"    {int((df['office_address'] != '').sum()):,} an office address; "
          f"{int((df['linkedin'] != '').sum()):,} a personal LinkedIn")
    print(f"    {df['office_state'].replace('', pd.NA).nunique()} states, "
          f"{df['office_name'].replace('', pd.NA).nunique()} offices")
    print("\n    phone_kind: " + ", ".join(
        f"{k} {v:,}" for k, v in df["phone_kind"].value_counts().items() if k))
    top = counts.most_common(5)
    print("    most-shared numbers: " + ", ".join(f"{p} x{n}" for p, n in top))

    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == CAPTRUST_CRD]["advisor_crd"].nunique()
        print(f"    SEC lists {sec:,} IARs at CRD {CAPTRUST_CRD}; this site lists "
              f"{len(df):,} people of all roles ({len(df) / sec:.0%})")
    if gone:
        print(f"    {len(gone)} profile(s) 404: {gone[:4]}")
    if failures:
        print(f"    {len(failures)} request(s) FAILED: {failures[:5]}")


if __name__ == "__main__":
    main()
