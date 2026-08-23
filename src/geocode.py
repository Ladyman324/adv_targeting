"""Tiered geocoding of advisor branch addresses.

  tier 1  US Census batch geocoder      free, no key, ~89% of addresses
  tier 2  validated neighbour fallback  free, recovers TIGER house-number gaps
  tier 3  commercial geocoder           config slot -- needs an API key

Every result carries `geocode_precision` so an interpolated coordinate can
never later be mistaken for a rooftop one:

  rooftop     tier 1, Census reported an exact match
  approximate tier 1, Census matched but flagged it non-exact
  neighbour   tier 2, snapped to the nearest validated house number on the
              same street and ZIP (typically a few metres; same building for
              a tower). The FILED address is always preserved separately --
              nothing here overwrites branch_street1/2.

Why tier 2 validates so strictly: sending a rewritten address to Census and
accepting any "Match" produces confidently wrong coordinates. Measured on the
Georgia failures, 13 of 372 "matched" and every one was a different street --
3280 PEACHTREE RD NE came back as PEACHTREE DR NE in another ZIP. So a
candidate is accepted only when the returned street line is character-equal
to the requested one and the ZIP agrees. Dropping the directional or the ZIP
to force a hit is deliberately not done; both were tested and returned
locations miles away.
"""
from __future__ import annotations

import csv
import io
import pathlib
import re
import sys
import time

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
from normalize_addr import normalize, pick_street  # noqa: E402

INTERIM = ROOT / "data" / "interim"
OUTPUT = ROOT / "data" / "output"
URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
BENCHMARK = "Public_AR_Current"
BATCH = 2_000        # well under the 10k cap; large chunks draw 502s under load
MAX_RETRY = 4
BACKOFF = 5          # seconds, doubled per attempt

# same-parity offsets first: even/odd house numbers sit on opposite kerbs
OFFSETS = [2, -2, 4, -4, 6, -6, 1, -1, 3, -3, 5, -5]

COLS = ["id", "input", "match", "matchtype", "matched", "lonlat", "tigerline", "side"]


def _post_chunk(rows: list[list[str]], attempt: int = 0) -> pd.DataFrame:
    """POST one chunk, retrying on 5xx / timeout. Census 502s intermittently."""
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow(r)
    try:
        resp = requests.post(URL, files={"addressFile": ("a.csv", buf.getvalue(), "text/csv")},
                             data={"benchmark": BENCHMARK}, timeout=600)
        resp.raise_for_status()
        return pd.read_csv(io.StringIO(resp.text), header=None, names=COLS, dtype=str)
    except (requests.HTTPError, requests.Timeout, requests.ConnectionError) as e:
        if attempt >= MAX_RETRY:
            print(f"      giving up on {len(rows)} rows after {attempt} retries: {e}")
            return pd.DataFrame(columns=COLS)
        wait = BACKOFF * (2 ** attempt)
        print(f"      {type(e).__name__} -- retry {attempt + 1}/{MAX_RETRY} in {wait}s")
        time.sleep(wait)
        return _post_chunk(rows, attempt + 1)


def _post(rows: list[list[str]]) -> pd.DataFrame:
    """POST rows in modest chunks. Smaller chunks survive Census load better."""
    out = []
    n = max(1, (len(rows) + BATCH - 1) // BATCH)
    for i in range(0, len(rows), BATCH):
        got = _post_chunk(rows[i:i + BATCH])
        out.append(got)
        print(f"    chunk {i // BATCH + 1}/{n}: {len(got):,} rows")
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=COLS)


def _split_house(street: str) -> tuple[str, str]:
    """'3438 PEACHTREE RD NE' -> ('3438', 'PEACHTREE RD NE')"""
    m = re.match(r"^(\d+)\s+(.*)$", street.strip())
    return (m.group(1), m.group(2).strip()) if m else ("", "")


def _returned_street(matched: str) -> tuple[str, str, str]:
    """'3436 PEACHTREE RD NE, ATLANTA, GA, 30326' -> ('3436','PEACHTREE RD NE','30326')"""
    parts = [p.strip() for p in str(matched).split(",")]
    if not parts or not parts[0]:
        return "", "", ""
    num, rest = _split_house(parts[0].upper())
    zc = parts[-1] if re.fullmatch(r"\d{5}(-\d{4})?", parts[-1]) else ""
    return num, rest, zc[:5]


def _coords(lonlat: str) -> tuple[float, float]:
    try:
        lon, lat = str(lonlat).split(",")
        return float(lat), float(lon)
    except Exception:
        return float("nan"), float("nan")


def _distinct(state: str):
    b = pd.read_parquet(OUTPUT / "advisor_branches.parquet")
    sub = b[(b["branch_state"] == state) & b["branch_street1"].notna()].copy()
    for c in ("branch_street1", "branch_city", "branch_postal"):
        sub[c] = sub[c].astype(str).str.strip()
    sub["addr_key"] = (sub["branch_street1"] + "|" + sub["branch_city"] +
                       "|" + state + "|" + sub["branch_postal"])
    sub["branch_street2"] = sub["branch_street2"].fillna("").astype(str)
    uniq = (sub[["addr_key", "branch_street1", "branch_street2", "branch_city", "branch_postal"]]
            .drop_duplicates("addr_key").reset_index(drop=True))
    uniq["id"] = uniq.index.astype(str)
    uniq["zip5"] = uniq["branch_postal"].str[:5]
    # line used for geocoding only -- falls back to line 2 when line 1 is a
    # building name. Filed lines are never modified.
    uniq["geo_street"] = [pick_street(a, b) for a, b in
                          zip(uniq["branch_street1"], uniq["branch_street2"])]
    swapped = (uniq["geo_street"] != "") & (uniq["branch_street1"].map(normalize) == "")
    if swapped.any():
        print(f"  note: {swapped.sum()} address(es) took the street from line 2")
    return sub, uniq


def _send_street(r) -> str:
    """The street line to hand the Census -- never an empty one.

    normalize() returns '' for anything without a leading house number, which
    is right for CHOOSING between line 1 and line 2 but wrong as a send filter.
    "ONE BRYANT PARK" is a real filed address the Census parses correctly by
    itself; blanking it silently dropped 13,266 addresses in the national run,
    including the Merrill, Edward Jones, Schwab, Fidelity and Stifel head
    offices. Measured on those, the Census matched 7 of 12 sampled HQs when
    sent verbatim. So: prefer the normalized line, then the line-2 fallback,
    then the filed line exactly as filed.
    """
    if normalize(r["branch_street1"]):
        return r["branch_street1"]
    if r["geo_street"]:
        return r["geo_street"]
    return str(r["branch_street1"] or "").strip() or str(r["branch_street2"] or "").strip()


def tier1(uniq: pd.DataFrame, state: str) -> pd.DataFrame:
    print("  tier 1 -- Census, filed address")
    rows = [[r["id"], _send_street(r), r["branch_city"], state, r["branch_postal"]]
            for _, r in uniq.iterrows()]
    res = _post(rows).merge(uniq[["id", "addr_key"]], on="id", how="left")
    res["lat"], res["lon"] = zip(*res["lonlat"].map(_coords))
    hit = res["match"].eq("Match") & res["lat"].notna()
    res["geocode_precision"] = None
    res.loc[hit & res["matchtype"].eq("Exact"), "geocode_precision"] = "rooftop"
    res.loc[hit & ~res["matchtype"].eq("Exact"), "geocode_precision"] = "approximate"
    ok = res[res["geocode_precision"].notna()]
    print(f"    matched {len(ok):,}/{len(res):,} = {len(ok)/max(len(res),1):.1%} "
          f"(rooftop {(res['geocode_precision']=='rooftop').sum():,}, "
          f"approximate {(res['geocode_precision']=='approximate').sum():,})")
    return res


def tier2(uniq: pd.DataFrame, failed_keys: set, state: str) -> pd.DataFrame:
    """Try neighbouring house numbers; accept only on an exact street+ZIP match."""
    todo = uniq[uniq["addr_key"].isin(failed_keys)].copy()
    todo["norm"] = todo["geo_street"]
    todo = todo[todo["norm"] != ""]
    print(f"  tier 2 -- neighbour fallback on {len(todo):,} addresses")
    if todo.empty:
        return pd.DataFrame(columns=["addr_key", "lat", "lon", "geocode_precision", "matched"])

    rows, meta = [], {}
    for _, r in todo.iterrows():
        num, rest = _split_house(r["norm"])
        if not num:
            continue
        for off in OFFSETS:
            n2 = int(num) + off
            if n2 <= 0:
                continue
            rid = f'{r["id"]}#{off}'
            rows.append([rid, f"{n2} {rest}", r["branch_city"], state, r["branch_postal"]])
            meta[rid] = (r["addr_key"], off, rest, r["zip5"])

    res = _post(rows)
    best = {}
    for _, r in res[res["match"].eq("Match")].iterrows():
        m = meta.get(r["id"])
        if not m:
            continue
        key, off, rest, zip5 = m
        _, ret_rest, ret_zip = _returned_street(r["matched"])
        if ret_rest != rest or ret_zip != zip5:     # strict: same street, same ZIP
            continue
        if key not in best or abs(off) < abs(best[key][0]):
            lat, lon = _coords(r["lonlat"])
            best[key] = (off, lat, lon, r["matched"])

    out = pd.DataFrame([{"addr_key": k, "lat": v[1], "lon": v[2],
                         "geocode_precision": "neighbour", "matched": v[3],
                         "neighbour_offset": v[0]} for k, v in best.items()])
    print(f"    recovered {len(out):,}/{len(todo):,} = {len(out)/max(len(todo),1):.1%} "
          f"(rejected as wrong street/ZIP: {res['match'].eq('Match').sum() - len(best):,} candidate hits)")
    return out


def geocode_state(state: str) -> pd.DataFrame:
    sub, uniq = _distinct(state)
    print(f"{state}: {len(sub):,} branch rows | {len(uniq):,} distinct addresses")

    t1 = tier1(uniq, state)
    good = t1[t1["geocode_precision"].notna()][
        ["addr_key", "lat", "lon", "geocode_precision", "matched"]]
    failed = set(uniq["addr_key"]) - set(good["addr_key"])

    t2 = tier2(uniq, failed, state)
    if not t2.empty:
        good = pd.concat([good, t2[["addr_key", "lat", "lon", "geocode_precision", "matched"]]],
                         ignore_index=True)

    still = len(uniq) - len(good)
    print(f"  TOTAL {len(good):,}/{len(uniq):,} = {len(good)/len(uniq):.1%} "
          f"| still unplaced {still:,}  (tier 3 / commercial geocoder)")

    pins = sub.merge(good, on="addr_key", how="left")
    adv = pd.read_parquet(OUTPUT / "advisors.parquet")[["advisor_crd", "first_name", "last_name"]]
    firms = pd.read_parquet(OUTPUT / "firms.parquet")[
        ["crd", "firm_display", "opportunity_score", "motion"]]
    firms["crd"] = firms["crd"].astype(str)
    pins["advisor_crd"] = pins["advisor_crd"].astype(str)
    pins["firm_crd"] = pins["firm_crd"].astype(str)
    pins = pins.merge(adv, on="advisor_crd", how="left").merge(
        firms, left_on="firm_crd", right_on="crd", how="left")

    INTERIM.mkdir(parents=True, exist_ok=True)
    pins.to_parquet(INTERIM / f"branch_geocoded_{state}.parquet", index=False)
    placed = pins["lat"].notna()
    print(f"  -> {placed.sum():,} advisor pins placed "
          f"({pins.loc[placed, 'advisor_crd'].nunique():,} distinct advisors)")
    print(pins.loc[placed, "geocode_precision"].value_counts().to_string())
    return pins


if __name__ == "__main__":
    geocode_state(sys.argv[1] if len(sys.argv) > 1 else "GA")
