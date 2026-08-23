"""Corient advisor roster -> data/raw/firm_rosters/corient_<date>.csv

    GET https://corient.com/us/en/meet-a-partner

One request. The page has a "Load More" button, but it is purely client-side:
the ENTIRE roster is already in the initial HTML, as an HTML-entity-escaped
JSON array inside a `data-initial-load` attribute on the listing component.

    data-initial-load="{&#34;specialties&#34;:[...],&#34;advisors&#34;:[...]}"

Unescape the attribute, parse it as JSON, and every advisor is there with a
work EMAIL and a biography. There is no API behind Load More -- no .json
endpoint, no GraphQL, no page-size parameter anywhere on the page -- so
clicking it would only re-reveal rows we already hold. Driving a browser for
this would be strictly slower and less reliable.

WHAT IS AND IS NOT HERE
-----------------------
Present: name, business title, work EMAIL (541 of 541), office city + state,
specialties, biography, and `legacyFirm`.
Absent: phone, street address, ZIP, lat/lon. City and state are the only
geography, so placement goes through the city centroid.

legacyFirm IS THE INTERESTING FIELD
-----------------------------------
Corient is the rebuilt US arm of CI Financial's acquisition spree, and this
field names the firm each advisor came in with -- Regent, BDF, Brightworth,
Eaton, RGT, CPWM and others. Brightworth in particular is an Atlanta practice.
Note the values are prefixed "CI - " and that a bare "CI -" (205 advisors)
means the parent, not a named acquisition; it is normalised to empty here so
nobody reads it as a firm called "CI".

Run:  python src/corient_async.py [--dry-run]
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

SOURCE = "https://corient.com/us/en/meet-a-partner"
CORIENT_CRD = "319448"
HEADERS = {
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
}
BLOB_RE = re.compile(r'data-initial-load="([^"]+)"')
COUNT_RE = re.compile(r"&#34;fullName&#34;")
RETRIES = 3
PAUSE = 1.5

COLUMNS = ["name", "first_name", "last_name", "title", "email", "city", "state",
           "location_label", "legacy_firm", "specialties", "bio", "headshot"]


def fetch() -> str:
    for attempt in range(RETRIES):
        try:
            r = requests.get(SOURCE, headers=HEADERS, timeout=90)
            if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                return r.text
            print(f"    [-] attempt {attempt + 1}: HTTP {r.status_code}")
        except Exception as exc:
            print(f"    [-] attempt {attempt + 1}: {type(exc).__name__} {exc}")
        time.sleep(PAUSE * (2 ** attempt) + random.random())
    raise SystemExit(f"could not fetch {SOURCE}")


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


def split_city(entry) -> tuple:
    """city is a list of {label, defaultLabel, id}. Returns (city, state, label).

    The label is usually 'Seattle, WA', but not always: three records carry a
    bare state ('California') and one carries a REGION ('Southeast'). Splitting
    naively puts 'California' in the city column and leaves the state empty,
    which is wrong twice over. So the raw label is always kept, and city/state
    are filled only when they can actually be read."""
    if not entry:
        return "", "", ""
    label = ((entry[0] or {}).get("label") or "").strip()
    if "," in label:
        city, _, state = label.rpartition(",")
        return city.strip(), state.strip(), label
    if label in STATE_NAMES:                 # a state, with no city
        return "", STATE_NAMES[label], label
    return "", "", label                     # a region such as "Southeast"


def legacy(value: str) -> str:
    """'CI - Brightworth' -> 'Brightworth'; a bare 'CI -' is the parent, not an
    acquisition, so it becomes empty rather than a firm named 'CI'."""
    cleaned = re.sub(r"^CI\s*-\s*", "", (value or "").strip()).strip()
    return "" if cleaned.upper() in ("", "CI") else cleaned


def normalise(rec: dict) -> dict:
    city, state, label = split_city(rec.get("city"))
    specialties = [s.get("label") or s.get("defaultLabel") or ""
                   for s in (rec.get("specialty") or []) if isinstance(s, dict)]
    return {
        "name": (rec.get("fullName") or "").strip(),
        "first_name": (rec.get("firstName") or "").strip(),
        "last_name": (rec.get("lastName") or "").strip(),
        # a few titles arrive with a trailing space ("Partner ")
        "title": (rec.get("businessTitle") or "").strip(),
        "email": (rec.get("email") or "").strip(),
        "city": city,
        "state": state,
        "location_label": label,
        "legacy_firm": legacy(rec.get("legacyFirm")),
        "specialties": "; ".join(s for s in specialties if s),
        "bio": re.sub(r"\s+", " ", (rec.get("bio") or "")).strip(),
        "headshot": rec.get("headshotAEM") or "",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    started = time.time()
    page = fetch()

    blob = BLOB_RE.search(page)
    if not blob:
        raise SystemExit("no data-initial-load attribute -- the page has changed")
    payload = json.loads(htmllib.unescape(blob.group(1)))
    advisors = payload.get("advisors") or []
    if not advisors:
        raise SystemExit("no advisors in the payload -- refusing to write an empty roster")

    # The attribute is one of several on the page; make sure the one parsed
    # holds every advisor the raw HTML mentions, rather than a first slice.
    on_page = len(COUNT_RE.findall(page))
    if on_page != len(advisors):
        print(f"    [!] {on_page} fullName occurrences in the HTML but "
              f"{len(advisors)} parsed -- there may be a second blob")

    df = pd.DataFrame([normalise(a) for a in advisors], columns=COLUMNS)
    print(f"[*] {len(df):,} advisors in {time.time() - started:.0f}s")
    if args.dry_run:
        print("[*] dry run: nothing written")
        return

    scratch_path("corient", "initial_load", ext="json").write_text(
        json.dumps(payload, indent=1), encoding="utf-8")
    out = roster_path("corient")
    df.to_csv(out, index=False)

    emails = int((df["email"] != "").sum())
    print(f"    roster -> {out}")
    print(f"    {emails:,} have an email ({emails / len(df):.1%}); "
          f"{int((df['bio'] != '').sum()):,} a bio; "
          f"{int((df['specialties'] != '').sum()):,} list a specialty")
    print(f"    NO phone, street address or coordinates -- city and state are "
          f"the only geography in this feed")
    print(f"    {df['state'].replace('', pd.NA).nunique()} states, "
          f"{df['city'].replace('', pd.NA).nunique()} cities")
    vague = df[df["city"] == ""]
    if len(vague):
        print(f"    {len(vague)} record(s) give no city -- a bare state or a "
              f"region: {sorted(set(vague['location_label']))}")
    named = df.loc[df["legacy_firm"] != "", "legacy_firm"].value_counts()
    print(f"    {int((df['legacy_firm'] != '').sum()):,} name an acquired firm "
          f"({len(named)} distinct): {dict(named.head(6))}")

    dupes = df[df["email"] != ""]["email"].str.lower().duplicated().sum()
    if dupes:
        print(f"    [!] {dupes} duplicate email(s) -- records should be unique")

    if BRANCHES.exists():
        b = pd.read_parquet(BRANCHES, columns=["advisor_crd", "firm_crd"])
        sec = b[b["firm_crd"].astype(str) == CORIENT_CRD]["advisor_crd"].nunique()
        print(f"    SEC lists {sec:,} IARs at CRD {CORIENT_CRD}; this page "
              f"publishes {len(df):,} ({len(df) / sec:.0%})")


if __name__ == "__main__":
    main()
