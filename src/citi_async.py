"""Citi Wealth Management advisor roster -> data/raw/firm_rosters/citi_<date>.csv

    GET https://wealthmanagement.citi.com/js/searchdata.json

A static JSON file behind the Find an Advisor page. One request, no search
parameters, no paging, no nonce -- the simplest source we hold by a distance.

READ THE COVERAGE NUMBER BEFORE USING THIS
------------------------------------------
229 advisors in 9 states, against 2,409 IARs in 27 states at CRD 7059. That is
9.5%, and unlike Baird or Truist the shortfall is NOT just the client-facing
distinction -- eighteen states where Citi files a branch have no published
advisor at all:

    published  CA DC FL IL MD NJ NV NY TX
    missing    AZ CO CT DE GA KY MA MO NC OH OK OR PA PR RI SC VA WY

wealthmanagement.citi.com is Citi Personal Wealth Management's own directory,
not a register of everyone at Citigroup Global Markets. Treat this roster as a
sample of Citi, never as its footprint: an advisor's absence here says nothing
about whether they exist.

WHAT IS AND IS NOT HERE
-----------------------
Present: name, title, role, street address, city, state, ZIP, phone, group
(team) name, profile URL, areas of focus (100 of 229), languages (56).
Absent: EMAIL and lat/lon. Citi is the only one of the recent five with no
email, so it cannot feed click-to-email; the ZIP centroid handles placement.

Run:  python src/citi_async.py
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from firm_rosters import roster_path, scratch_path  # one naming convention, defined once

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRANCHES = ROOT / "data" / "output" / "advisor_branches.parquet"

SOURCE = "https://wealthmanagement.citi.com/js/searchdata.json"
PROFILE_BASE = "https://wealthmanagement.citi.com/"
CITI_CRD = "7059"
HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "accept-language": "en-US,en;q=0.9",
    "x-requested-with": "XMLHttpRequest",
    "referer": "https://wealthmanagement.citi.com/find-an-advisor/",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
}
RETRIES = 3
PAUSE = 1.0

COLUMNS = ["name", "first_name", "last_name", "title", "role", "phone",
           "address1", "address2", "city", "state", "postal", "team_name",
           "areas_of_focus", "languages", "profile_url", "handle", "n_addresses"]


def fetch() -> list:
    for attempt in range(RETRIES):
        try:
            r = requests.get(SOURCE, headers=HEADERS, timeout=90)
            if r.status_code == 200:
                return r.json().get("advisors") or []
            print(f"    [-] HTTP {r.status_code}")
        except Exception as exc:
            print(f"    [-] {type(exc).__name__}: {exc}")
        time.sleep(PAUSE * (2 ** attempt) + random.random())
    raise SystemExit(f"could not fetch {SOURCE}")


def normalise(rec: dict) -> dict:
    addresses = rec.get("addresses") or []
    # Nine advisors list more than one office; the first is the one the site
    # shows, so it is the one we place them at. The count is kept so a later
    # matcher can see that a single address is not the whole story.
    home = addresses[0] if addresses else {}
    path = (rec.get("profilePaths") or {}).get("en") or ""
    return {
        "name": (rec.get("fullName") or "").strip(),
        "first_name": rec.get("firstName") or "",
        "last_name": rec.get("lastName") or "",
        "title": rec.get("title") or "",
        "role": rec.get("role") or "",
        "phone": rec.get("phone1") or "",
        "address1": home.get("street1") or "",
        "address2": home.get("street2") or "",
        "city": home.get("city") or "",
        "state": home.get("state") or "",
        # zip arrives as an int for some records and a string for others
        "postal": str(home.get("zip") or ""),
        "team_name": rec.get("groupName") or "",
        "areas_of_focus": "; ".join(rec.get("areasOfFocus") or []),
        "languages": "; ".join(rec.get("additionalLanguages") or []),
        "profile_url": f"{PROFILE_BASE}{path}" if path else "",
        "handle": rec.get("handle") or "",
        "n_addresses": len(addresses),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    args = ap.parse_args()

    started = time.time()
    advisors = fetch()
    if not advisors:
        raise SystemExit("no advisors in the payload -- refusing to write an empty roster")

    df = pd.DataFrame([normalise(a) for a in advisors], columns=COLUMNS)
    print(f"[*] {len(df):,} advisors in {time.time() - started:.0f}s")
    if args.dry_run:
        print("[*] dry run: nothing written")
        return

    scratch_path("citi", "api", ext="json").write_text(
        json.dumps(advisors, indent=1), encoding="utf-8")
    out = roster_path("citi")
    df.to_csv(out, index=False)

    print(f"    roster -> {out}")
    print(f"    {int((df['phone'] != '').sum()):,} have a phone; "
          f"NO EMAIL in this feed; no coordinates either")
    print(f"    {df['state'].nunique()} states, "
          f"{df['team_name'].replace('', pd.NA).nunique()} groups, "
          f"{int((df['areas_of_focus'] != '').sum()):,} list areas of focus, "
          f"{int((df['languages'] != '').sum()):,} list a second language")

    dupes = len(df) - df["handle"].nunique()
    if dupes:
        print(f"    [!] {dupes} duplicate handle(s) -- the payload should be unique")

    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd", "branch_state"])
        b = b[b["firm_crd"].astype(str) == CITI_CRD]
        sec_states = set(b["branch_state"].dropna())
        missing = sorted(sec_states - set(df["state"]))
        print(f"    SEC lists {b['advisor_crd'].nunique():,} IARs at CRD {CITI_CRD} "
              f"across {len(sec_states)} states; this directory publishes "
              f"{len(df):,} across {df['state'].nunique()} "
              f"({len(df) / b['advisor_crd'].nunique():.0%})")
        if missing:
            # Unlike the other rosters, this gap is geographic, not just
            # client-facing-vs-registered. Say so every run so it is never
            # mistaken for full coverage.
            print(f"    [!] {len(missing)} state(s) where Citi files a branch have "
                  f"NO published advisor: {' '.join(missing)}")


if __name__ == "__main__":
    main()
