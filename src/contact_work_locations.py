"""Build source-backed working offices for approved contact identities.

This layer is separate from the SEC branch filing. A firm roster (or, absent
one, the approved ACT! record) may tell us where a contact actually works.
Only an exact email/ACT-id link to an approved CRD AND a current SEC firm pair
can change a map pin or sales territory. The original filing remains intact.

Run after build_contacts.py and before placement.py:
    python src/contact_work_locations.py
"""
from __future__ import annotations

import collections
import argparse
import os
import glob
import json
import math
import pathlib
import re

import pandas as pd

from build_contacts import load_rosters, newest_act_pull
from geography_integrity import USPS_REGIONS, state_agrees
from geocode import _post_chunk, _coords, _returned_street
from geocode_google import fetch as google_fetch, judge as google_judge, components as google_components

ROOT = pathlib.Path(__file__).resolve().parents[1]
INTERIM = ROOT / "data" / "interim"
WEB = ROOT / "webapp" / "data"
OUT = INTERIM / "contact_work_branches.parquet"
AUDIT = ROOT / "data" / "output" / "contact_work_locations.csv"
CACHE = INTERIM / "contact_work_geocodes.csv"
GOOGLE_CACHE = INTERIM / "contact_work_google_cache.jsonl"
ZIP = re.compile(r"^\d{5}(?:-\d{4})?$")


def placement_key(street: object, city: object, postal: object) -> str:
    """The exact three-part key used by placement and both map exporters."""
    return (str(street or "").strip().upper() + "|"
            + str(city or "").strip().upper() + "|"
            + str(postal or "").strip()[:5])


def clean_street(raw: object, city: str, state: str, postal: str) -> str:
    value = " ".join(str(raw or "").split()).strip(" ,")
    # Edward Jones publishes a single full-address string. A pin's street
    # field must not repeat the city/state/ZIP already stored separately.
    tail = re.compile(
        rf",?\s*{re.escape(city)}\s*,\s*{re.escape(state)}\s+"
        rf"{re.escape(postal)}\s*$", re.I)
    return tail.sub("", value).strip(" ,")


def miles(a: float, b: float, c: float, d: float) -> float:
    r1, r2 = math.radians(a), math.radians(c)
    dr, dl = r2 - r1, math.radians(d - b)
    h = math.sin(dr / 2) ** 2 + math.cos(r1) * math.cos(r2) * math.sin(dl / 2) ** 2
    return 3959 * 2 * math.asin(min(1, math.sqrt(h)))


def source_record(row: dict, source: str, zips: dict) -> dict | None:
    city = str(row.get("city") or "").strip()
    state = str(row.get("state") or "").strip().upper()
    postal = str(row.get("location_zip") or "").strip()
    street = clean_street(row.get("location_street"), city, state, postal)
    if not (city and state in USPS_REGIONS and ZIP.fullmatch(postal) and street):
        return None
    zip5 = postal[:5]
    centroid = zips.get(zip5)
    if not centroid or centroid[0] != state:
        return None
    try:
        lat = float(row.get("location_lat") or "nan")
        lon = float(row.get("location_lon") or "nan")
    except (TypeError, ValueError):
        lat = lon = math.nan
    # A source coordinate may be an HQ or a parsing mistake. The ZIP
    # centroid is only a sanity check, never itself used as a map pin.
    if (math.isfinite(lat) and math.isfinite(lon)
            and miles(lat, lon, float(centroid[1]), float(centroid[2])) > 60):
        lat = lon = math.nan
    return {"branch_street1": street, "branch_city": city,
            "branch_state": state, "branch_postal": postal,
            "lat": lat, "lon": lon, "location_source": source,
            "coordinate_origin": "published" if math.isfinite(lat) else ""}


def select_candidates(contacts: dict, rosters: pd.DataFrame, act: list,
                      employment: set, zips: dict) -> tuple[pd.DataFrame, list]:
    """A unique email plus firm agreement, then approved ACT-id fallback."""
    by_email: dict[str, set[str]] = collections.defaultdict(set)
    for crd, c in contacts.items():
        email = str(c.get("e") or "").strip().lower()
        if email:
            by_email[email].add(str(crd))
    unique = {email: next(iter(ids)) for email, ids in by_email.items()
              if len(ids) == 1
              and contacts[next(iter(ids))].get("t") in ("confirmed", "high")}
    roster_names: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    for email, firm, name in rosters[["email", "firm_crd", "name"]].itertuples(
            index=False, name=None):
        address = str(email or "").strip().lower()
        if address:
            roster_names[(address, str(firm or ""))].add(
                str(name or "").strip().lower())
    candidates: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    audit = []
    for row in rosters.to_dict("records"):
        email = str(row.get("email") or "").strip().lower()
        crd = unique.get(email)
        if not crd:
            continue
        firm = str(row.get("firm_crd") or "")
        if (crd, firm) not in employment or len(roster_names[(email, firm)]) > 1:
            continue
        location = source_record(row, "firm_roster", zips)
        if location:
            location.update(advisor_crd=crd, firm_crd=firm,
                            source_file=str(row.get("source_file") or ""))
            candidates[(crd, firm)].append(location)

    chosen = {}
    for pair, options in candidates.items():
        # One published office, possibly repeated on a team page, is safe.
        # Multiple different offices for the same person are not a default.
        locations = {(o["branch_street1"].upper(), o["branch_city"].upper(),
                      o["branch_state"], o["branch_postal"][:5]) for o in options}
        if len(locations) != 1:
            audit.append({"advisor_crd": pair[0], "firm_crd": pair[1],
                          "result": "ambiguous_roster_offices"})
            continue
        options.sort(key=lambda o: math.isfinite(o["lat"]), reverse=True)
        chosen[pair] = options[0]

    act_by_id = {str(row.get("id") or ""): row for row in act}
    for crd, c in contacts.items():
        if c.get("src") != "CRM" or c.get("t") != "confirmed":
            continue
        firm = str(c.get("fc") or "")
        pair = (str(crd), firm)
        if pair in chosen or pair not in employment:
            continue
        raw = act_by_id.get(str(c.get("aid") or ""))
        if not raw or str(raw.get("emailAddress") or "").strip().lower() != str(c.get("e") or "").strip().lower():
            continue
        address = raw.get("businessAddress") or {}
        location = source_record({
            "city": address.get("city"), "state": address.get("state"),
            "location_street": address.get("line1"),
            "location_zip": address.get("postalCode"),
            "location_lat": address.get("latitude"),
            "location_lon": address.get("longitude"),
        }, "ACT", zips)
        if location:
            location.update(advisor_crd=str(crd), firm_crd=firm,
                            source_file="approved ACT JSON")
            chosen[pair] = location
    return pd.DataFrame(chosen.values()), audit


def existing_coordinates(candidates: pd.DataFrame, branches: pd.DataFrame) -> pd.DataFrame:
    """Reuse a geocoded SEC street only when state, city, ZIP and street agree."""
    key = ["branch_street1", "branch_city", "branch_state", "branch_postal"]
    def addr(frame):
        return (frame[key[0]].fillna("").astype(str).str.upper().str.strip()
                + "|" + frame[key[1]].fillna("").astype(str).str.upper().str.strip()
                + "|" + frame[key[2]].fillna("").astype(str).str.upper().str.strip()
                + "|" + frame[key[3]].fillna("").astype(str).str[:5])
    source = branches[branches["lat"].notna() & branches["lon"].notna()].copy()
    source["_loc_key"] = addr(source)
    coords = source.drop_duplicates("_loc_key").set_index("_loc_key")[["lat", "lon"]]
    missing = candidates["lat"].isna()
    keys = addr(candidates.loc[missing])
    for col in ("lat", "lon"):
        candidates.loc[missing, col] = keys.map(coords[col]).to_numpy()
    resolved = missing & candidates["lat"].notna()
    candidates.loc[resolved, "coordinate_origin"] = "sec_exact_address"
    return candidates


def free_geocode(candidates: pd.DataFrame, *, allow_network: bool = False) -> pd.DataFrame:
    """Census batch geocode, cached; reject cross-state/ZIP/house-number moves."""
    missing = candidates[candidates["lat"].isna()].copy()
    if missing.empty:
        return candidates
    fields = ["branch_street1", "branch_city", "branch_state", "branch_postal"]
    missing["_loc_key"] = (
        missing[fields[0]].str.upper().str.strip() + "|"
        + missing[fields[1]].str.upper().str.strip() + "|"
        + missing[fields[2]] + "|"
        + missing[fields[3]].str[:5])
    unique = missing.drop_duplicates("_loc_key").copy()
    cached = pd.read_csv(CACHE, dtype=str).fillna("") if CACHE.exists() else pd.DataFrame()
    known = set(cached["_loc_key"]) if len(cached) else set()
    todo = unique[~unique["_loc_key"].isin(known)]
    fresh = []
    print(f"  free Census geocode: {len(todo):,} new addresses; {len(known):,} cached")
    if not allow_network:
        todo = todo.iloc[:0]
    for start in range(0, len(todo), 2000):
        chunk = todo.iloc[start:start + 2000]
        records = [[str(i), r.branch_street1, r.branch_city,
                    r.branch_state, r.branch_postal]
                   for i, r in enumerate(chunk.itertuples(index=False))]
        result = _post_chunk(records)
        lookup = dict(enumerate(chunk.to_dict("records")))
        for hit in result.to_dict("records"):
            original = lookup.get(int(hit["id"]))
            if original is None:
                continue
            lat, lon = _coords(hit.get("lonlat", ""))
            number = re.match(r"^\s*(\d+)", original["branch_street1"])
            got_number, _, got_zip = _returned_street(hit.get("matched", ""))
            valid = (hit.get("match") == "Match"
                     and math.isfinite(lat) and math.isfinite(lon)
                     and state_agrees(hit.get("matched"), original["branch_state"])
                     and got_zip == original["branch_postal"][:5]
                     and (not number or number.group(1) == got_number))
            fresh.append({"_loc_key": original["_loc_key"],
                          "lat": lat if valid else "", "lon": lon if valid else ""})
        # Missing responses are cached as unresolved to avoid repeated calls.
        completed = {r["_loc_key"] for r in fresh}
        for record in chunk.to_dict("records"):
            if record["_loc_key"] not in completed:
                fresh.append({"_loc_key": record["_loc_key"], "lat": "", "lon": ""})
        pd.DataFrame(fresh).to_csv(CACHE, index=False)
        print(f"    Census batch {start // 2000 + 1}: "
              f"{sum(bool(r['lat']) for r in fresh):,} resolved so far")
    if len(cached):
        all_cache = pd.concat([cached, pd.DataFrame(fresh)], ignore_index=True)
        all_cache = all_cache.drop_duplicates("_loc_key", keep="last")
        all_cache.to_csv(CACHE, index=False)
    else:
        all_cache = pd.DataFrame(fresh)
    if len(all_cache):
        geo = all_cache.set_index("_loc_key")
        keys = missing["_loc_key"]
        for col in ("lat", "lon"):
            values = pd.to_numeric(keys.map(geo[col]), errors="coerce")
            candidates.loc[missing.index, col] = values.to_numpy()
        resolved = missing.index[candidates.loc[missing.index, "lat"].notna()]
        candidates.loc[resolved, "coordinate_origin"] = "census"
    return candidates


def google_geocode(candidates: pd.DataFrame, *, max_calls: int) -> pd.DataFrame:
    """Bounded last resort; retain only unflagged street-level Google results."""
    missing = candidates[candidates["lat"].isna()].copy()
    if missing.empty:
        return candidates
    def identity(row):
        return "|".join((row["branch_street1"].upper().strip(),
                         row["branch_city"].upper().strip(),
                         row["branch_state"], row["branch_postal"][:5]))
    missing["_loc_key"] = missing.apply(identity, axis=1)
    unique = missing.drop_duplicates("_loc_key")
    cache = {}
    if GOOGLE_CACHE.exists():
        for line in GOOGLE_CACHE.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            cache[record["key"]] = record
    todo = unique[~unique["_loc_key"].isin(cache)]
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if max_calls and len(todo) and not key:
        print("  Google key absent; only cached results will be used")
        max_calls = 0
    print(f"  Google fallback: {len(todo):,} new addresses, "
          f"{len(cache):,} cached, cap {max_calls:,} calls")
    GOOGLE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    for row in todo.head(max_calls).to_dict("records"):
        query = (f"{row['branch_street1']}, {row['branch_city']}, "
                 f"{row['branch_state']} {row['branch_postal']}")
        body = google_fetch(query, key)
        record = {"key": row["_loc_key"], "status": body.get("status"),
                  "results": body.get("results", [])[:1]}
        with GOOGLE_CACHE.open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, separators=(",", ":")) + "\n")
        cache[row["_loc_key"]] = record
    resolved = {}
    for row in unique.to_dict("records"):
        record = cache.get(row["_loc_key"])
        if not record:
            continue
        body = {"status": record["status"], "results": record["results"]}
        filed = {"street": row["branch_street1"], "city": row["branch_city"],
                 "state": row["branch_state"], "zip": row["branch_postal"][:5]}
        lat, lon, _, name, _, check = google_judge(filed, body)
        result = body["results"][0] if body["results"] else {}
        got_zip = google_components(result).get("postal_code", "")[:5]
        if (lat is not None and not check
                and state_agrees(name, filed["state"])
                and got_zip == filed["zip"]):
            resolved[row["_loc_key"]] = (lat, lon)
    for index, row in missing.iterrows():
        point = resolved.get(row["_loc_key"])
        if point:
            candidates.at[index, "lat"], candidates.at[index, "lon"] = point
            candidates.at[index, "coordinate_origin"] = "google"
    print(f"    Google accepted {len(resolved):,} distinct exact-address results")
    return candidates


def main(*, census: bool = False, google_max_calls: int = 0) -> None:
    contacts = json.loads((WEB / "contacts.json").read_text(encoding="utf-8"))["advisors"]
    zips = json.loads((WEB / "geo_index.json").read_text(encoding="utf-8"))["zips"]
    employment = pd.read_parquet(ROOT / "data/output/advisor_employments.parquet",
                                 columns=["advisor_crd", "firm_crd"])
    pairs = set(zip(employment["advisor_crd"].astype(str),
                    employment["firm_crd"].astype(str)))
    act_path = newest_act_pull()
    act = json.loads(act_path.read_text(encoding="utf-8")) if act_path else []
    candidates, audit = select_candidates(contacts, load_rosters(), act, pairs, zips)
    if candidates.empty:
        raise RuntimeError("no approved source-backed working locations")
    # Use the existing SEC branch rows only as a carrier for firm scoring and
    # other advisor metadata. Their coordinates/address are replaced below.
    ids = set(candidates["advisor_crd"])
    frames = []
    for path in sorted(glob.glob(str(INTERIM / "branch_geocoded_*.parquet"))):
        frame = pd.read_parquet(path)
        sub = frame[frame["advisor_crd"].astype(str).isin(ids)]
        if len(sub):
            frames.append(sub)
    branches = pd.concat(frames, ignore_index=True)
    branches["advisor_crd"] = branches["advisor_crd"].astype(str)
    branches["firm_crd"] = branches["firm_crd"].astype(str)
    candidates = existing_coordinates(candidates, branches)
    candidates = free_geocode(candidates, allow_network=census)
    candidates = google_geocode(candidates, max_calls=google_max_calls)
    # Ungeocoded addresses remain in the audit; they cannot silently inherit
    # the old SEC pin or a ZIP/city centroid as a fake street location.
    missing = candidates[candidates["lat"].isna()]
    for row in missing.itertuples():
        audit.append({"advisor_crd": row.advisor_crd, "firm_crd": row.firm_crd,
                      "result": "needs_free_geocode", "source": row.location_source,
                      "state": row.branch_state, "city": row.branch_city,
                      "street": row.branch_street1, "zip": row.branch_postal})
    candidates = candidates[candidates["lat"].notna() & candidates["lon"].notna()].copy()
    templates = branches.drop_duplicates(["advisor_crd", "firm_crd"])
    selected = templates.merge(candidates, on=["advisor_crd", "firm_crd"],
                               how="inner", suffixes=("", "_work"))
    for col in ("branch_street1", "branch_city", "branch_state",
                "branch_postal", "lat", "lon"):
        selected[col] = selected.pop(col + "_work")
    selected["branch_street2"] = ""
    selected["location_source"] = selected["location_source"].astype(str)
    selected["matched"] = (selected["location_source"] + ": "
                           + selected["branch_street1"] + ", "
                           + selected["branch_city"] + ", "
                           + selected["branch_state"] + " "
                           + selected["branch_postal"])
    selected["geocode_precision"] = selected["coordinate_origin"].map(
        {"published": "source_published",
         "sec_exact_address": "approximate",
         "census": "approximate", "google": "approximate"})
    selected["geocode_source"] = selected["coordinate_origin"]
    selected["addr_key"] = [
        placement_key(street, city, postal)
        for street, city, postal in zip(
            selected["branch_street1"], selected["branch_city"],
            selected["branch_postal"])]
    selected.to_parquet(OUT, index=False)
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(audit, columns=["advisor_crd", "firm_crd", "result",
                                  "source", "state", "city", "street", "zip"]).to_csv(AUDIT, index=False)
    print(f"working locations: {len(selected):,} source-backed pins "
          f"({selected['location_source'].value_counts().to_dict()}); "
          f"{len(missing):,} need geocoding; audit {AUDIT}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--census", action="store_true",
                        help="Send unresolved roster/ACT street addresses to the public Census geocoder")
    parser.add_argument("--google-max-calls", type=int, default=0,
                        help="Maximum Google calls for remaining addresses; requires GOOGLE_MAPS_API_KEY")
    args = parser.parse_args()
    main(census=args.census, google_max_calls=args.google_max_calls)
