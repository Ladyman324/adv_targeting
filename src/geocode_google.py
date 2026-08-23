"""Tier 3: resolve the addresses the Census could not place, via Google.

The Census TIGER file is a street-range database. It cannot place a named
tower ("50 HUDSON YARDS", "ONE PPG PLACE"), a private campus road, or a house
number that was never filed into a range. Those are few -- 8,618 distinct
addresses -- but they carry 29,125 pins and 27,513 advisors, and they cluster
at exactly the large multi-tenant offices worth prospecting.

BUDGET IS THE BINDING CONSTRAINT, NOT TIME.
Google's free allowance is 10,000 Geocoding calls per month and we need
~8,618, a margin of about 14%. One accidental re-run would blow it. So:

  * every response is appended to a JSONL cache the instant it arrives, and
    a cached address is never requested again -- kill this mid-run and
    restart it and it resumes, having spent nothing on what it already has
  * a hard --max-calls ceiling, checked before each request
  * --dry-run reports the plan and spends zero calls

VALIDATION, and why it is strict.
Google always answers. Ask for a house number it does not know and it will
happily return the street, the ZIP centroid, or the city -- the same failure
that made Nominatim return "Salem Street, Smithfield" for 900 SALEM STREET.
A confidently wrong coordinate is worse than a null one, because nothing
downstream can tell it apart. So a result is accepted only when:

  * the returned state matches the filed state, AND
  * the returned ZIP5 or locality matches what we filed, AND
  * result.types is a real address-level type -- a bare `locality`,
    `postal_code` or `administrative_area` result is a centroid and is
    rejected outright, however confident the response looks

location_type maps to the project's existing precision vocabulary, so a
centroid can never be read back later as a rooftop:

    ROOFTOP            -> rooftop
    RANGE_INTERPOLATED -> approximate      (same idea as Census interpolation)
    GEOMETRIC_CENTER   -> approximate      (parcel/segment centre)
    APPROXIMATE        -> approximate

THE KEY IS NEVER STORED BY THIS SCRIPT. It is read from the environment and
is not written to the cache, the output table, or any log line.

    setx GOOGLE_MAPS_API_KEY "..."        (once, then reopen the shell)
    python src\\geocode_google.py --dry-run
    python src\\geocode_google.py --run
    python src\\apply_overrides.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
from datetime import date

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).parents[1]
INTERIM = ROOT / "data" / "interim"
REFERENCE = ROOT / "data" / "reference"
CACHE = INTERIM / "google_geocode_cache.jsonl"
OVERRIDES = REFERENCE / "address_overrides.csv"

URL = "https://maps.googleapis.com/maps/api/geocode/json"
PAUSE = 0.05          # ~20/s; well inside Google's limit, polite either way
MAX_RETRY = 4
BACKOFF = 2           # seconds, doubled per attempt

# Below the free monthly allowance of 10,000, deliberately. The gap absorbs a
# partial re-run without tipping into paid usage.
DEFAULT_MAX_CALLS = 9_500

PRECISION = {
    "ROOFTOP": "rooftop",
    "RANGE_INTERPOLATED": "approximate",
    "GEOMETRIC_CENTER": "approximate",
    "APPROXIMATE": "approximate",
}

# A result carrying only these is a centroid, not an address. Accepting one
# puts an office in the middle of a city and calls it geocoded.
CENTROID_TYPES = {
    "locality", "sublocality", "postal_code", "postal_code_prefix",
    "administrative_area_level_1", "administrative_area_level_2",
    "administrative_area_level_3", "country", "political", "neighborhood",
}


def unplaced() -> pd.DataFrame:
    """Unplaced addresses, one row per FILED variant, carrying a shared call_key.

    Two levels of distinctness, and conflating them is expensive both ways:

    `call_key` is the uppercased address -- one Google call per real building.
    "50 HUDSON YARDS" and "50 Hudson Yards" are the same place, and paying
    twice for it wastes 438 of a 9,500-call budget.

    But apply_overrides.py joins on the filed string exactly (`key_d.eq(k)`),
    so the output table still needs a row per casing variant, all sharing the
    one resolved coordinate. Collapsing them here would silently leave the
    lowercase pins unplaced.
    """
    frames = []
    for f in sorted(INTERIM.glob("branch_geocoded_*.parquet")):
        d = pd.read_parquet(f, columns=[
            "advisor_crd", "lat", "branch_street1", "branch_street2",
            "branch_city", "branch_state", "branch_postal", "firm_display"])
        d["state"] = f.stem.replace("branch_geocoded_", "")
        frames.append(d[d["lat"].isna()])
    if not frames:
        sys.exit(f"no geocoded parquets in {INTERIM}")

    a = pd.concat(frames, ignore_index=True)
    a["street"] = a["branch_street1"].astype(str).str.strip()
    a["city"] = a["branch_city"].astype(str).str.strip()
    a["zip"] = a["branch_postal"].astype(str).str[:5]
    a = a[a["street"].ne("") & a["street"].ne("nan")]

    g = (a.groupby(["state", "street", "city", "zip"], as_index=False)
           .agg(pins=("advisor_crd", "size"),
                advisors=("advisor_crd", "nunique"),
                firm=("firm_display", lambda s: s.mode().iloc[0] if len(s) else "")))
    g["call_key"] = (g["street"].str.upper() + "|" + g["city"].str.upper()
                     + "|" + g["state"].str.upper() + "|" + g["zip"])
    # cost of a call_key is the pins across all its variants, so the --limit
    # pilot spends on the genuinely biggest buildings
    g["call_pins"] = g.groupby("call_key")["pins"].transform("sum")
    return g.sort_values(["call_pins", "call_key"], ascending=[False, True]) \
            .reset_index(drop=True)


def load_cache() -> dict:
    """Everything already answered. Append-only JSONL, so a crash costs nothing."""
    out = {}
    if CACHE.exists():
        with CACHE.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue          # truncated final line after a hard kill
                out[rec["key"]] = rec
    return out


def append_cache(rec: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with CACHE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def fetch(addr: str, key: str, attempt: int = 0) -> dict:
    """One call. Returns the parsed body; retries transient failures only."""
    try:
        resp = requests.get(
            URL, params={"address": addr, "key": key, "region": "us"}, timeout=30)
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        if attempt >= MAX_RETRY:
            return {"status": "TRANSPORT_ERROR", "error_message": str(exc)}
        time.sleep(BACKOFF * (2 ** attempt))
        return fetch(addr, key, attempt + 1)

    if body.get("status") in ("OVER_QUERY_LIMIT", "UNKNOWN_ERROR"):
        if attempt >= MAX_RETRY:
            return body
        time.sleep(BACKOFF * (2 ** attempt))
        return fetch(addr, key, attempt + 1)
    return body


def components(result: dict) -> dict:
    """Flatten address_components to {type: short_name}."""
    out = {}
    for c in result.get("address_components", []):
        for t in c.get("types", []):
            out.setdefault(t, c.get("short_name", ""))
    return out


# Filed addresses spell small house numbers as words often enough to matter --
# ONE PPG PLACE, ONE WILLIAMS CENTER, One Post Office Square all resolve
# correctly and must not be read as "no house number".
WORD_NUM = {"ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4", "FIVE": "5",
            "SIX": "6", "SEVEN": "7", "EIGHT": "8", "NINE": "9", "TEN": "10"}


GRID = re.compile(r"^[NSEW]\d+$", re.I)          # Milwaukee-area grid: "N14 W23833"


def filed_house_no(street: str) -> str:
    """Leading house number of a filed line; '' if the line has none.

    Handles three real formats seen in the filings:
      "1100 Ridgeway"          -> 1100
      "ONE PPG PLACE"          -> 1
      "N14 W23833 Stone Ridge" -> N14W23833   (Milwaukee grid, spelled either
                                 as two tokens or one; Google returns it
                                 closed up, so normalise to that)
    """
    tok = [t.rstrip(",") for t in str(street or "").strip().split()]
    if not tok:
        return ""
    head = tok[0].upper()
    if head.isdigit():
        return head
    if head in WORD_NUM:
        return WORD_NUM[head]
    if GRID.match(head):
        if len(tok) > 1 and GRID.match(tok[1].upper()):
            return head + tok[1].upper()
        return head
    if re.match(r"^[NSEW]\d+[NSEW]\d+$", head, re.I):
        return head
    return ""


# A result carrying these matched a BUSINESS, not an address. Google resolved
# the filed line "CITI PRIVATE BANK" to 3 Greenwich Office Park and reported it
# ROOFTOP -- probably correct, but it matched a name, not the street we filed,
# and nothing downstream could tell. Compare with the Milwaukee grid address
# "N14 W23833 STONE RIDGE DR", which comes back types=[street_address] and is
# verifiable. So the types decide, not the confidence.
POI_TYPES = {"establishment", "point_of_interest"}

# Google treats these as countries, not states. See the state check in judge().
TERRITORIES = {"PR", "VI", "GU", "MP", "AS"}


def house_check(row, comp: dict, name: str):
    """Does the returned building number match the one we filed?

    location_type is not enough on its own. Google returned ROOFTOP with high
    confidence for "135 US HIGHWAY 202/206", having parsed the /206 as a unit
    and invented house number 13; and for "CITI PRIVATE BANK", which is a firm
    name, not an address, matched to a business POI. Both are plausible-looking
    coordinates that no downstream check would question.

    So this compares house numbers and FLAGS rather than rejects -- a mismatch
    is often still the right building (200 Vesey St comes back under Merrill's
    10080 rather than the filed 10281) and throwing away 83 pins on a formatting
    quirk is its own kind of error. Flagged rows keep their coordinate, lose
    their rooftop claim, and carry a check value so they can be reviewed.
    """
    want = filed_house_no(row["street"]).replace(" ", "").upper()
    got = str(comp.get("street_number") or "").strip().replace(" ", "").upper()
    if not want:
        return "", "", False                           # nothing to check against
    if not got:
        return "STREET LEVEL", f"no house number returned (filed {want})", False
    if got != want and want not in got.split("-"):
        return "HOUSE NUMBER MISMATCH", f"filed {want}, google returned {got}", False
    return "", "", True                                # numbers agree -- confirmed


def judge(row, body: dict):
    """(lat, lon, precision, matched_name, note, check).

    lat is None for a rejection; `check` is non-empty for a coordinate that is
    kept but doubted. Rejection is a first-class outcome, recorded with its
    reason, so the table shows what was tried and refused rather than
    silently omitting it.
    """
    if body.get("status") != "OK" or not body.get("results"):
        return None, None, None, "", f"google status {body.get('status', '?')}", "REJECTED"

    res = body["results"][0]
    name = res.get("formatted_address", "")
    types = set(res.get("types", []))

    if types and types.issubset(CENTROID_TYPES):
        return None, None, None, name, \
            f"centroid result ({','.join(sorted(types))})", "REJECTED"

    comp = components(res)
    want_state = str(row["state"]).upper()
    # For the territories Google puts the code in `country` and the MUNICIPALITY
    # in administrative_area_level_1 -- San Juan, Guaynabo, Ponce, St Thomas.
    # Comparing aal1 to the filed state rejected 71 of 73 territory addresses as
    # "state mismatch" when every one of them was correct.
    if want_state in TERRITORIES:
        got_state = (comp.get("country") or "").upper()
    else:
        got_state = (comp.get("administrative_area_level_1") or "").upper()
    if got_state and got_state != want_state:
        return None, None, None, name, f"state mismatch: got {got_state}", "REJECTED"

    got_zip = (comp.get("postal_code") or "")[:5]
    got_city = (comp.get("locality") or comp.get("sublocality") or "").upper()
    want_city = str(row["city"]).upper()
    if got_zip and row["zip"] and got_zip != row["zip"]:
        if not got_city or got_city != want_city:
            return None, None, None, name, \
                f"zip and city mismatch: got {got_zip} {got_city}", "REJECTED"

    loc = res.get("geometry", {}).get("location", {})
    lat, lon = loc.get("lat"), loc.get("lng")
    if lat is None or lon is None:
        return None, None, None, name, "no geometry in result", "REJECTED"

    lt = res.get("geometry", {}).get("location_type", "APPROXIMATE")
    prec = PRECISION.get(lt, "approximate")
    note = "google " + lt.lower()

    check, why, confirmed = house_check(row, comp, name)
    # A matching house number is positive confirmation and outranks the POI
    # type: PPG Place and One Williams Center are tagged `establishment` by
    # Google but resolved to exactly the number we filed. Only flag a business
    # match when there was no house number to confirm it with.
    if not check and not confirmed and (types & POI_TYPES):
        check = "POI MATCH"
        why = f"matched a business, not the filed street ({','.join(sorted(types & POI_TYPES))})"
    if check:
        # a doubted coordinate must never keep a rooftop claim -- downstream
        # reads precision, not the check column
        prec = "approximate"
        note = note + "; " + why
    return round(float(lat), 6), round(float(lon), 6), prec, name, note, check


def merge_out(new: pd.DataFrame) -> None:
    """Fold into the override table WITHOUT clobbering hand-verified rows.

    build_overrides.py overwrote this file, which would have discarded the
    nine google_maps_manual rows that were looked up by hand. Existing rows
    always win; only genuinely new addresses are added.
    """
    cols = ["state", "street", "city", "zip", "lat", "lon", "source",
            "verified_on", "matched_name", "pins", "firm", "note",
            "precision", "check"]
    new = new.reindex(columns=cols)

    if OVERRIDES.exists():
        old = pd.read_csv(OVERRIDES, dtype=str).fillna("")
        old = old.reindex(columns=cols)

        # Rows this script produced are derived from the cache and are always
        # replaced, so tightening a validation rule re-grades the existing
        # results for free. Rows from anywhere else -- nominatim, and the nine
        # looked up by hand on Google Maps -- cannot be re-derived and are
        # never touched.
        mine = old["source"].eq("google_geocoding_api")
        dropped = int(mine.sum())
        old = old[~mine]

        idx = ["state", "street", "city", "zip"]
        have = set(map(tuple, old[idx].apply(lambda s: s.str.upper()).values.tolist()))
        keep = [tuple(str(v).upper() for v in r) not in have
                for r in new[idx].values.tolist()]
        added = new[keep]
        out = pd.concat([old, added], ignore_index=True)
        print(f"\n{len(old)} hand-verified rows kept, {dropped} prior API rows "
              f"re-graded, {len(added)} rows written")
    else:
        out = new
        print(f"\n{len(out)} rows written")

    OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OVERRIDES, index=False)
    print(f"-> {OVERRIDES}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="spend calls; otherwise dry run")
    ap.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    ap.add_argument("--limit", type=int, default=0, help="only the N costliest, for a pilot")
    args = ap.parse_args()

    todo = unplaced()
    cache = load_cache()

    # one call per real address; --limit counts CALLS, not filed variants
    calls = todo.drop_duplicates("call_key")
    if args.limit:
        calls = calls.head(args.limit)
        todo = todo[todo["call_key"].isin(set(calls["call_key"]))]
    fresh = calls[~calls["call_key"].isin(cache)]

    print(f"{len(todo):,} filed address variants "
          f"({todo['pins'].sum():,} pins, {todo['advisors'].sum():,} advisors)")
    print(f"{len(calls):,} distinct addresses after case-folding "
          f"({len(todo) - len(calls):,} variants ride along free)")
    print(f"{len(cache):,} already cached -> {len(fresh):,} calls needed "
          f"(ceiling {args.max_calls:,})")

    if len(fresh) > args.max_calls:
        print(f"\nWould exceed the ceiling. Raise --max-calls, or use --limit to "
              f"take the costliest {args.max_calls:,} first.")

    if not args.run:
        print("\nDRY RUN -- no calls made. Costliest 10 pending:")
        print(fresh.head(10)[["state", "street", "city", "zip", "call_pins", "firm"]]
              .to_string(index=False))
        return

    # Only needed if there is something to fetch. Re-grading what is already
    # cached costs nothing and must not require a key -- that is how the
    # validation rules get tightened without re-spending the budget.
    api_key = ""
    if len(fresh):
        api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
        if not api_key:
            sys.exit("GOOGLE_MAPS_API_KEY is not set in this shell -- see the module "
                     "docstring. The key is read from the environment and never stored.")
    else:
        print("nothing new to fetch -- re-grading the cache, 0 calls")

    spent = 0
    for _, r in fresh.iterrows():
        if spent >= args.max_calls:
            print(f"\nceiling of {args.max_calls:,} reached -- stopping cleanly. "
                  f"Re-run to continue; nothing already fetched will be re-requested.")
            break
        addr = f"{r['street']}, {r['city']}, {r['state']} {r['zip']}"
        body = fetch(addr, api_key)
        spent += 1
        append_cache({"key": r["call_key"], "query": addr,
                      "status": body.get("status"),
                      "results": body.get("results", [])[:1]})
        if spent % 250 == 0:
            print(f"  {spent:,} calls...")
        time.sleep(PAUSE)

    print(f"\n{spent:,} calls spent this run; cache now {len(load_cache()):,} addresses")

    cache = load_cache()
    rows, ok, rejected = [], 0, 0
    for _, r in todo.iterrows():
        rec = cache.get(r["call_key"])
        if not rec:
            continue
        body = {"status": rec.get("status"), "results": rec.get("results", [])}
        lat, lon, prec, name, note, check = judge(r, body)
        if lat is not None:
            ok += 1
        else:
            rejected += 1
        rows.append({
            "state": r["state"], "street": r["street"], "city": r["city"],
            "zip": r["zip"], "lat": lat, "lon": lon, "source": "google_geocoding_api",
            "verified_on": date.today().isoformat(), "matched_name": name,
            "pins": r["pins"], "firm": r["firm"], "note": note,
            "precision": prec, "check": check,
        })

    new = pd.DataFrame(rows)
    print(f"\n{ok:,} accepted, {rejected:,} rejected")
    if ok:
        got = new[new["lat"].notna()]
        print(f"pins recovered: {got['pins'].sum():,}")
        print(got["precision"].value_counts().to_string())
        flagged = got[got["check"].ne("")]
        if len(flagged):
            print(f"\n{len(flagged)} kept but FLAGGED for review "
                  f"({flagged['pins'].sum():,} pins) -- coordinate retained, "
                  f"precision downgraded:")
            print(flagged[["state", "street", "city", "matched_name", "check"]]
                  .to_string(index=False))
    if rejected:
        print("\nrejection reasons:")
        print(new[new["lat"].isna()]["note"].value_counts().head(10).to_string())

    merge_out(new)
    print("\nnext:  python src\\apply_overrides.py --apply")


if __name__ == "__main__":
    main()
