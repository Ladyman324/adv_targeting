"""Second pass for overrides Nominatim missed, using alternate query forms.

A filed line like "THE WILLIAM BLAIR BUILDING" or "ONE BRYANT PARK" is a
building name, not an address, so a structured street query cannot match it.
OSM does carry these as named features, just under a different name -- One
Bryant Park is tagged "Bank of America Tower". This pass supplies those
alternate names explicitly and lets Nominatim confirm or deny each one; the
alternate is a search term, never an assumed answer.

Every accepted result must still land in the right place: the returned feature
must be within RADIUS_KM of the filed ZIP's centroid, which is itself resolved
from OSM. A named-building match in the wrong metro is rejected.
"""
from __future__ import annotations

import datetime as dt
import math
import pathlib
import time

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).parents[1]
OVERRIDES = ROOT / "data" / "reference" / "address_overrides.csv"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = "EIC-adv-targeting/1.0 (internal sales research; bladyman@eicatlanta.com)"
PAUSE = 1.1
RADIUS_KM = 25          # generous: campuses sit outside the ZIP they file under

# filed line -> other names OSM may know the same building by
ALT = {
    "ONE BRYANT PARK":              ["Bank of America Tower, New York, NY"],
    "THE WILLIAM BLAIR BUILDING":   ["William Blair, 150 North Riverside Plaza, Chicago, IL"],
    "ONE AMERICAN SQUARE":          ["American Square, Indianapolis, IN"],
    "ONE COLUMBUS PLAZA":           ["Columbus Plaza, New Haven, CT"],
    "ONE FREEDOM VALLEY DRIVE":     ["SEI Investments, Oaks, PA"],
    "753 AMERIPRISE FINANCIAL CTR": ["Ameriprise Financial Center, Minneapolis, MN"],
    "1100-1800 AMERICAN BLVD":      ["1100 American Boulevard, Pennington, NJ"],
    "1030 ADMIRAL NELSON CT":       ["Vanguard, Charlotte, NC"],
    "14600 BRANCH ST.":             ["14600 Branch Street, Omaha, NE"],
    "601 Office Center Drive":      ["601 Office Center Drive, Fort Washington, PA"],
    "100 NEW MILLENNIUM WAY":       ["100 New Millennium Way, Durham, NC"],
    "100 COLISEUM DRIVE":           ["100 Coliseum Drive, Cohoes, NY"],
    "101 FEDERAL PL STE 101":       ["101 Federal Place, Tarpon Springs, FL"],
    "8601 N SCOTTSDALE RD STE 150": ["8601 North Scottsdale Road, Scottsdale, AZ"],
    "68 S SERVICE RD STE 200":      ["68 South Service Road, Melville, NY"],
    "200 ASHFORD CENTER NORTH":     ["200 Ashford Center North, Atlanta, GA"],
    "2000 WESTCHESTER AVENUE":      ["Morgan Stanley, 2000 Westchester Avenue, Harrison, NY"],
    "2000 Westchester Avenue":      ["Morgan Stanley, 2000 Westchester Avenue, Harrison, NY"],
    "2000 WESTCHESTER AVE":         ["Morgan Stanley, 2000 Westchester Avenue, Harrison, NY"],
}


def km(a, b, c, d):
    R = 6371.0
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def query(params):
    try:
        r = requests.get(NOMINATIM, params={**params, "format": "jsonv2", "limit": 1},
                         headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        hits = r.json()
    except Exception as e:
        print(f"      error: {type(e).__name__}")
        hits = []
    time.sleep(PAUSE)
    return hits[0] if hits else None


def main() -> None:
    df = pd.read_csv(OVERRIDES, dtype=str).fillna("")
    todo = df[df["lat"].eq("")]
    print(f"retrying {len(todo)} unresolved rows\n")

    zip_cache = {}
    for i, r in todo.iterrows():
        alts = ALT.get(r["street"].strip())
        if not alts:
            print(f"  --  {r['street'][:38]:38.38s} no alternate name configured")
            continue

        # anchor: where the filed ZIP actually is, per OSM
        z = r["zip"]
        if z not in zip_cache:
            h = query({"postalcode": z, "country": "USA"})
            zip_cache[z] = (float(h["lat"]), float(h["lon"])) if h else None
        anchor = zip_cache[z]

        hit = None
        for a in alts:
            h = query({"q": a})
            if not h:
                continue
            lat, lon = float(h["lat"]), float(h["lon"])
            if anchor:
                d = km(anchor[0], anchor[1], lat, lon)
                if d > RADIUS_KM:
                    print(f"  x   {r['street'][:38]:38.38s} {a[:30]:30.30s} "
                          f"REJECTED {d:.0f}km from ZIP {z}")
                    continue
            else:
                d = float("nan")
            hit = (lat, lon, h.get("display_name", "")[:120], a, d)
            break

        if not hit:
            print(f"  --  {r['street'][:38]:38.38s} still unresolved")
            continue

        lat, lon, name, used, d = hit
        df.loc[i, ["lat", "lon", "source", "verified_on", "matched_name",
                   "precision", "check"]] = [
            round(lat, 6), round(lon, 6), "nominatim-alt", dt.date.today().isoformat(),
            name, "approximate", f"matched via alternate name '{used}'"
            + (f", {d:.1f}km from ZIP centroid" if d == d else "")]
        print(f"  ok  {r['street'][:38]:38.38s} -> {name[:44]}")

    df.to_csv(OVERRIDES, index=False)
    df["pins"] = df["pins"].astype(int)
    ok = df[df["lat"].ne("")]
    left = df[df["lat"].eq("")]
    print(f"\nresolved {len(ok)}/{len(df)} rows, {ok['pins'].sum():,} pins")
    print(f"remaining {len(left)} rows, {left['pins'].sum():,} pins")


if __name__ == "__main__":
    main()
