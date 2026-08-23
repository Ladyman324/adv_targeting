"""Mercer Advisors roster -> data/raw/firm_rosters/mercer_<date>.csv

Three stages, all JSON except the last:

  list      GET /wp-json/team/v1/list            1 request -> 1,203 members
  member    GET /wp-json/team/v1/member/<id>     role, designations, phone, bio
  bio       GET /wp-json/bio/v1/member/<id>      which office they sit in
  location  GET /location/<slug>/                office address + office phone

The site exposes purpose-built endpoints rather than only wp/v2 (which 403s on
`team`), so one call returns the entire roster with EMAIL attached, and the
per-member calls fill in everything else. No HTML parsing for the people at all
-- only the ~90 location pages are scraped, once each, and cached across members.

DIRECT VS OFFICE, SETTLED BY THE SOURCE
---------------------------------------
This firm does not need the count-the-occupants heuristic, because it keeps the
two facts in different places:

    team/v1/member/<id>.phone_number   the PERSON's own line, when published
    /location/<slug>/ JSON-LD telephone the OFFICE switchboard

They are written to `direct_phone` and `office_phone` and never merged. In a
twelve-person sample only 2 published a personal number, so most of this roster
is office-reachable only -- and the columns say which is which per row.

THREE NUMBERS ON A PROFILE PAGE, ONLY ONE OF THEM PERSONAL
----------------------------------------------------------
Scraping the HTML instead would have been a trap. Jennifer Adams' page shows:

    828.285.8777   her Asheville OFFICE (inside the JSON-LD workLocation)
    888.920.1320   the "Let's Talk" number in the site FOOTER, on every page
    814-980-5065   NOT A PHONE -- a reCAPTCHA form condition value

A phone-shaped regex over that page returns all three.

Run:  python src/mercer_async.py [--dry-run] [--refresh] [--limit N]
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

BASE = "https://www.merceradvisors.com"
API = BASE + "/wp-json/"
MERCER_CRD = "147363"
HEADERS = {
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
    "accept": "application/json, text/html;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "referer": BASE + "/meet-our-team/",
}
PAUSE = 0.25
RETRIES = 3
TOLLFREE = {"800", "833", "844", "855", "866", "877", "888"}

LD_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
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


def get(session, url, want_json=True):
    for attempt in range(RETRIES):
        try:
            r = session.get(url, headers=HEADERS, timeout=90)
            kind = r.headers.get("content-type", "")
            if r.status_code == 200:
                if want_json and kind.startswith("application/json"):
                    return r.json()
                if not want_json and "text/html" in kind:
                    return r.text
            if r.status_code == 404:
                return "gone"
            print(f"    [-] attempt {attempt + 1}: HTTP {r.status_code} {url}")
        except Exception as exc:
            print(f"    [-] attempt {attempt + 1}: {type(exc).__name__} {exc}")
        time.sleep(PAUSE * (2 ** attempt) + random.random())
    return None


def office_details(session, url, cache):
    """Address and switchboard for one office, from its JSON-LD. Cached: a
    hundred people share ninety offices, so this is fetched once each."""
    if url in cache:
        return cache[url]
    out = {"street": "", "city": "", "state": "", "postal": "", "phone": ""}
    markup = get(session, url, want_json=False)
    if isinstance(markup, str):
        for blob in LD_RE.findall(markup):
            try:
                node = json.loads(blob)
            except Exception:
                continue
            addr = node.get("address") if isinstance(node, dict) else None
            if isinstance(addr, dict) and addr.get("@type") == "PostalAddress":
                region = text(addr.get("addressRegion") or "")
                out = {"street": text(addr.get("streetAddress") or ""),
                       "city": text(addr.get("addressLocality") or ""),
                       "state": STATE_NAMES.get(region, region[:2].upper() if region else ""),
                       "postal": text(addr.get("postalCode") or ""),
                       "phone": text(str(node.get("telephone") or ""))}
                break
    cache[url] = out
    time.sleep(PAUSE)
    return out


COLUMNS = ["name", "role", "designations", "email", "direct_phone",
           "direct_phone_kind", "office", "office_street", "office_city",
           "office_state", "office_postal", "office_phone", "office_url",
           "profile_url", "headshot", "bio", "member_id"]


def classify_direct(value):
    """A personal number can still be a toll-free routing line -- Mercer has at
    least one (888.885.8101) -- so label rather than assume."""
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if digits[:1] == "1":
        digits = digits[1:]
    return "toll-free" if digits[:3] in TOLLFREE else "direct"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="fetch the list only")
    ap.add_argument("--refresh", action="store_true", help="ignore the cached list")
    ap.add_argument("--limit", type=int, help="only the first N members, for a trial")
    args = ap.parse_args()

    started = time.time()
    session = requests.Session()
    failures = []

    cache_file = scratch_path("mercer", "list", ext="json")
    if cache_file.exists() and not args.refresh:
        members = json.loads(cache_file.read_text(encoding="utf-8"))
        print(f"[*] {len(members):,} members from cache; --refresh to re-fetch")
    else:
        members = get(session, API + "team/v1/list")
        if not isinstance(members, list) or not members:
            raise SystemExit("team/v1/list returned nothing -- refusing to continue")
        cache_file.write_text(json.dumps(members, indent=1), encoding="utf-8")
        print(f"[*] team/v1/list: {len(members):,} members in one request")

    if args.dry_run:
        with_email = sum(1 for m in members if m.get("email"))
        print(f"[*] dry run: {with_email:,} already carry an email; "
              f"would fetch 2 detail calls each")
        return
    if args.limit:
        members = members[:args.limit]

    rows, offices = [], {}
    for i, m in enumerate(members, 1):
        mid = m.get("id")
        detail = get(session, f"{API}team/v1/member/{mid}")
        bio = get(session, f"{API}bio/v1/member/{mid}")
        if not isinstance(detail, dict):
            failures.append(str(mid))
            continue
        loc_name = loc_url = ""
        if isinstance(bio, dict):
            loc = bio.get("team_location") or {}
            if loc:
                first = list(loc.values())[0]
                if isinstance(first, list) and len(first) >= 2:
                    loc_name, loc_url = first[0], first[1]
        off = office_details(session, loc_url, offices) if loc_url else {
            "street": "", "city": "", "state": "", "postal": "", "phone": ""}
        direct = text(str(detail.get("phone_number") or ""))
        rows.append({
            "name": text(detail.get("name") or m.get("name") or ""),
            "role": text(detail.get("member_role") or ""),
            "designations": text(detail.get("certificate") or ""),
            "email": (detail.get("email") or m.get("email") or "").strip(),
            "direct_phone": direct,
            "direct_phone_kind": classify_direct(direct),
            "office": loc_name,
            "office_street": off["street"],
            "office_city": off["city"],
            "office_state": off["state"],
            "office_postal": off["postal"],
            "office_phone": off["phone"],
            "office_url": loc_url,
            "profile_url": detail.get("permalink") or m.get("permalink") or "",
            "headshot": detail.get("headshot") or "",
            "bio": text(detail.get("bio_copy") or ""),
            "member_id": str(mid),
        })
        if i % 200 == 0:
            print(f"    enriched {i:,}/{len(members):,}  ({len(offices)} offices cached)")
        time.sleep(PAUSE)

    if not rows:
        raise SystemExit("nothing collected -- refusing to write an empty roster")

    df = pd.DataFrame(rows, columns=COLUMNS)
    scratch_path("mercer", "members", ext="json").write_text(
        json.dumps(rows, indent=1), encoding="utf-8")
    out = roster_path("mercer")
    df.to_csv(out, index=False)

    emails = int((df["email"] != "").sum())
    direct = int((df["direct_phone"] != "").sum())
    offp = int((df["office_phone"] != "").sum())
    print(f"\n[*] {len(df):,} people in {time.time() - started:.0f}s -> {out}")
    print(f"    {emails:,} have a personal EMAIL ({emails / len(df):.1%})")
    print(f"    {direct:,} publish a DIRECT phone ({direct / len(df):.1%}); "
          f"of those {int((df['direct_phone_kind'] == 'toll-free').sum())} are toll-free")
    print(f"    {offp:,} have an OFFICE phone from their location page "
          f"({df.loc[df['office_phone'] != '', 'office_phone'].nunique()} distinct "
          f"numbers across {df['office'].replace('', pd.NA).nunique()} offices)")
    print(f"    {int((df['office_street'] != '').sum()):,} an office address; "
          f"{df['office_state'].replace('', pd.NA).nunique()} states; "
          f"{int((df['bio'] != '').sum()):,} bios")
    shared = Counter(df.loc[df["office_phone"] != "", "office_phone"])
    print("    most-shared office numbers: " +
          ", ".join(f"{p} x{n}" for p, n in shared.most_common(4)))
    no_loc = int((df["office"] == "").sum())
    if no_loc:
        print(f"    {no_loc:,} have no office assigned (corporate/remote roles)")

    dupes = len(df) - df["member_id"].nunique()
    if dupes:
        print(f"    [!] {dupes} duplicate member id(s)")
    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == MERCER_CRD]["advisor_crd"].nunique()
        print(f"    SEC lists {sec:,} IARs at CRD {MERCER_CRD}; this site lists "
              f"{len(df):,} people of all roles ({len(df) / sec:.0%})")
    if failures:
        print(f"    {len(failures)} member(s) FAILED: {failures[:6]}")


if __name__ == "__main__":
    main()
