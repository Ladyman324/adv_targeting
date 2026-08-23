"""Geographic tiles for the field view's "near me", written to webapp/data/tiles/.

WHY TILES AND NOT STATE SHARDS
------------------------------
The map already shards pins by state, and a territory scope is the union of its
states -- so sharding contacts the same way would have been consistent. It is
also the wrong key for this job, and the data says so plainly. "Near me" is a
RADIUS, and a radius does not respect a state line. Counting advisors within 25
miles of where a rep might be standing:

    Washington DC     9,123 nearby, 1,695 in DC          81% missed
    Kansas City MO    6,232 nearby, 1,294 in Missouri    79% missed
    Portland OR       4,739 nearby, 3,137 in Oregon      34% missed
    Cincinnati OH     5,151 nearby, 3,545 in Ohio        31% missed
    New York NY      37,300 nearby, 27,761 in New York   26% missed

A rep standing in DC would have seen 1,695 of 9,123. That is not a rough edge
at the border, it is the feature not working. Cells do not know where borders
are, so tiling by latitude/longitude removes the problem by construction rather
than patching around it.

WHY 0.25 DEGREES
----------------
Measured across the 123,379 advisors who have both a contact record and a
location:

    cell size        populated cells    median advisors    p95
    0.25 (~17 mi)    3,758              5                  151
    0.50 (~34 mi)    1,866              9                  308
    1.00 (~69 mi)    725                30                 859

A near-me fetch pulls the rep's cell plus its 8 neighbours. At 0.25 degrees
that is a few dozen records across most of the country. The distribution is
heavily skewed -- Manhattan's densest cell holds ~3,100 -- and that outlier is
accepted rather than engineered around: it is a few hundred KB gzipped, in the
one place a rep is most likely to have good signal.

RECORDS ARE ARRAYS, NOT OBJECTS
-------------------------------
Repeating sixteen key names across 123,379 records costs more than the data.
COLUMNS below is the contract; the client reads it from the file rather than
hardcoding positions, so adding a field later does not silently shift meaning.

Run:  python src/build_field_tiles.py
"""
from __future__ import annotations

import collections
import glob
import json
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import pandas as pd

from web_assets import write_json_gz

ROOT = pathlib.Path(__file__).resolve().parents[1]
INTERIM = ROOT / "data" / "interim"
WEB = ROOT / "webapp" / "data"
TILES = WEB / "tiles"
# Team rosters for the field view, one file per state. See where they are
# written for why they are sharded rather than shipped whole.
PRACTICES = WEB / "practices"

CELL = 0.25

# The contract between this file and the field view. Read from the payload by
# the client -- never hardcoded there.
COLUMNS = ["crd", "name", "title", "firm", "city", "state", "lat", "lon",
           "email", "phone", "phone_pretty", "phone_kind", "mobile",
           "team", "team_size", "team_key", "owner", "tier",
           # Added for the field view's chips and badges. A rep with ninety
           # minutes needs a reason to pick, and distance alone is not one --
           # the nearest advisor is often nobody in particular.
           "ranked",      # in Barron's or Forbes: a recognised, larger book
           "assets",      # dollars EIC already has with them, 0 if none
           "office"]      # street|city|zip5 -- who else is in this building


def cell_of(lat: float, lon: float) -> str:
    return f"{int(round(lat / CELL))}_{int(round(lon / CELL))}"


def load_positions() -> pd.DataFrame:
    """One position per advisor.

    An advisor can be filed at several branches and several firms. placement.py
    already decided which pairing is the real one for the map; this reuses that
    decision rather than inventing a second answer, then keeps a single row per
    PERSON -- the field view shows a human once, not once per registration.
    """
    frames = [pd.read_parquet(p, columns=["advisor_crd", "firm_crd", "lat", "lon",
                                          "branch_street1", "branch_city",
                                          "branch_state", "branch_postal"])
              for p in glob.glob(str(INTERIM / "branch_geocoded_*.parquet"))]
    geo = pd.concat(frames, ignore_index=True).dropna(subset=["lat", "lon"])
    for col in ("advisor_crd", "firm_crd"):
        geo[col] = geo[col].astype(str)

    # addr_key is RECOMPUTED, not read. The stored column in the geocoded files
    # is "STREET|CITY|STATE|99503-1234" while placement.py keys on
    # "STREET|CITY|99503" -- four parts against three, and a full ZIP against
    # five digits. Joining on the stored column silently matches NOTHING, which
    # is what it did: 0 rows out of 506. export_geojson rebuilds the key for the
    # same reason; this mirrors it rather than inventing a third format.
    geo["addr_key"] = (geo["branch_street1"].fillna("").astype(str).str.strip().str.upper()
                       + "|"
                       + geo["branch_city"].fillna("").astype(str).str.strip().str.upper()
                       + "|"
                       + geo["branch_postal"].fillna("").astype(str).str.strip().str[:5])

    place = pd.read_parquet(INTERIM / "advisor_placement.parquet")
    for col in ("advisor_crd", "firm_crd", "addr_key"):
        place[col] = place[col].astype(str)

    merged = geo.merge(place, on=["advisor_crd", "firm_crd", "addr_key"],
                       how="inner")
    # Prefer a corroborated office over an inferred home address when someone
    # has both, then take one row per advisor.
    merged["rank"] = merged["uncertain"].fillna(False).astype(bool).astype(int)
    merged = merged.sort_values("rank").drop_duplicates("advisor_crd", keep="first")
    print(f"[*] positions: {len(merged):,} advisors with a placed location")
    return merged[["advisor_crd", "lat", "lon", "branch_city", "branch_state",
                   "addr_key"]]


def load_ranked() -> set:
    """Advisor CRDs carrying a Barron's or Forbes ranking.

    A flag, not the ranking itself: the field view needs "is this one worth the
    detour", and the desktop map already renders the detail for anyone who
    wants it. One byte per record instead of a nested list per record.
    """
    out = set()
    for name in ("barrons.json", "forbes.json"):
        path = WEB / name
        if path.exists():
            out |= set(json.loads(path.read_text(encoding="utf-8"))
                       .get("advisors", {}))
    return out


def main() -> None:
    contacts = json.loads((WEB / "contacts.json").read_text(encoding="utf-8"))
    advisors = contacts["advisors"]
    teams = contacts.get("teams", {})
    practices = contacts.get("practices", {})
    # crd -> the display name the desktop shows. See the comment where it is used.
    index_names = {str(r[0]): r[1] for r in
                   json.loads((WEB / "advisor_index.json").read_text(encoding="utf-8"))["advisors"]}
    ranked = load_ranked()
    print(f"[*] contacts.json: {len(advisors):,} advisors")
    print(f"[*] ranked by Barron's or Forbes: {len(ranked & set(advisors)):,}")

    pos = load_positions()
    pos = pos[pos["advisor_crd"].isin(advisors)]
    print(f"[*] {len(pos):,} of those have contact detail AND a location")

    buckets: dict = collections.defaultdict(list)
    for crd, lat, lon, city, state, office in zip(
            pos["advisor_crd"], pos["lat"], pos["lon"], pos["branch_city"],
            pos["branch_state"], pos["addr_key"]):
        c = advisors[crd]
        team = c.get("tn", "")
        # How many people are in it. A team NAME tells a rep nothing about
        # whether they are looking at a solo practitioner or the entrance to a
        # twelve-person buying unit, and on a phone there is no room for the
        # full roster the desktop panel unfolds -- so the count travels and the
        # names do not.
        size = 0
        if c.get("pk") in practices:
            size = int(practices[c["pk"]].get("sz", 0) or 0)
        if not team and c.get("tm") in teams:
            team = teams[c["tm"]].get("n", "")
            size = size or int(teams[c["tm"]].get("sz", 0) or 0)
        # A team's assets belong to the TEAM and are stored once; an `ia` is
        # this person's own book. Never summed together -- carrying whichever
        # applies keeps the field view from repeating the double-count the
        # whole team model exists to prevent.
        money = float(c.get("ia", 0) or 0)
        if not money and c.get("tm") in teams:
            money = float(teams[c["tm"]].get("a", 0) or 0)
        buckets[cell_of(lat, lon)].append([
            crd,
            # THE DESKTOP'S NAME, not the contact record's.
            #
            # This used to take contacts.json's `n`, which is whichever CRM or
            # roster row won pick_best -- and the desktop map builds its name
            # from the SEC feed instead. 47,371 advisors carried a different
            # name in the two applications, so a rep who read "Cosmo Boyd" on
            # the desk searched the phone for it and found nobody: he is
            # Montague Boyd in the CRM.
            #
            # advisor_index.json is the desktop's own artifact and is written
            # AFTER reconcile_display_names.py has had its say, so taking the
            # name from here means the phone shows exactly what the desk shows,
            # including the roster-corroborated corrections.
            index_names.get(str(crd)) or c.get("n", ""),
            c.get("ti", ""),
            c.get("cn", ""),
            # City and state come from the PLACED office rather than the
            # contact record: the field view is answering "who is near me",
            # so the address that put the pin here is the right one to show.
            str(city or "").title(),
            str(state or "").upper(),
            round(float(lat), 5),
            round(float(lon), 5),
            c.get("e", ""),
            c.get("w", ""),
            c.get("wd", ""),
            c.get("wk", ""),
            c.get("c", ""),
            team,
            size,
            c.get("pk", "") if c.get("pk") in practices else "",
            c.get("o", ""),
            c.get("t", ""),
            1 if crd in ranked else 0,
            round(money),
            str(office or ""),
        ])

    if TILES.exists():
        # Rebuilt wholesale. A tile left behind from a previous run would serve
        # advisors who have since moved cell -- the same stale-data failure this
        # project has hit three times already, in a fourth location.
        shutil.rmtree(TILES)
    TILES.mkdir(parents=True, exist_ok=True)

    sizes = []
    for key, rows in buckets.items():
        rows.sort(key=lambda r: r[1])
        payload = {"cell": key, "columns": COLUMNS, "n": len(rows), "rows": rows}
        path = TILES / f"{key}.json"
        path.write_text(json.dumps(payload, separators=(",", ":")),
                        encoding="utf-8")
        sizes.append((len(rows), path.stat().st_size))

    counts = sorted(n for n, _ in sizes)
    total = sum(b for _, b in sizes)
    n = len(counts)
    # A list of populated cells, so the client never requests one that does not
    # exist -- in sparse areas 8 of the 9 neighbours are empty, and 8 wasted
    # round trips on a phone is a slow "near me" for no reason.
    write_json_gz(WEB / "tile_index.json",
                  {"cell": CELL, "columns": COLUMNS,
                   "cells": sorted(buckets)},
                  separators=(",", ":"))

    # WHO ELSE IS ON THAT TEAM, for the field view.
    #
    # The desktop panel resolves teammates out of contacts.json, which the phone
    # never loads. Shipping the practices whole is 668 KB gzipped -- too much to
    # hand a rep standing in a car park for a disclosure they may not open.
    #
    # Sharded by STATE instead, because that is what the field view already
    # knows about the advisor whose sheet is open: a rep in Georgia fetches
    # Georgia. Median shard is 10 KB and the largest -- Florida -- is 87 KB. A
    # practice with members in two states appears in both, which is correct
    # rather than wasteful: either rep should see the whole team.
    shard: dict = collections.defaultdict(dict)
    for key, rec in practices.items():
        entry = {"n": rec.get("n", ""),
                 "m": [[crd, index_names.get(str(crd)) or advisors.get(crd, {}).get("n", ""), st]
                       for crd, st in rec.get("m", [])]}
        for _, _, st in entry["m"]:
            if st:
                shard[st][key] = entry
    if PRACTICES.exists():
        shutil.rmtree(PRACTICES)
    PRACTICES.mkdir(parents=True, exist_ok=True)
    for state, records in shard.items():
        write_json_gz(PRACTICES / f"{state}.json", records, separators=(",", ":"))
    print(f"[*] {len(shard)} practice shards, "
          f"{sum(len(v) for v in shard.values()):,} practice entries")

    print(f"[*] {n:,} tiles written, {sum(counts):,} advisors, "
          f"{total / 1e6:.1f} MB total")
    print(f"    advisors per tile: median {counts[n // 2]}, "
          f"p95 {counts[int(n * 0.95)]}, max {counts[-1]:,}")
    print(f"    tile_index.json lists {n:,} populated cells")
    flagged = sum(1 for rows in buckets.values() for r in rows if r[16])
    monied = sum(1 for rows in buckets.values() for r in rows if r[17])
    print(f"    {flagged:,} ranked, {monied:,} carrying an EIC asset figure")


if __name__ == "__main__":
    main()
