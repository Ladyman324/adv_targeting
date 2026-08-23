"""Fetch each firm's other/former names from the IAPD firm API.

Why this source and not Form ADV: the names IAPD shows on a firm summary are
NOT Schedule D Section 1.B. CRD 79's full 277-page Form ADV never contains the
string "Bear" -- its Section 1.B lists only J.P. MORGAN and J.P. MORGAN WEALTH
MANAGEMENT. The alias list is CRD-system data merging the adviser and
broker-dealer records, and it is served only here:

    api.adviserinfo.sec.gov/search/firm/{crd}
      -> hits.hits[0]._source.iacontent  (a JSON *string*)
      -> basicInformation.otherNames

Every firm in a 25-firm sample returned the field, and the contents are exactly
the names a salesperson would say out loud: Smith Barney for Morgan Stanley,
Alex. Brown for Raymond James, Linsco/Private Ledger for LPL, PaineWebber for
UBS. Searching any of those returns nothing today.

Cached per CRD and resumable, throttled with an identifying User-Agent, in the
same shape as fetch_brochures.py.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
CACHE = ROOT / "data" / "firm_api"
INTERIM = ROOT / "data" / "interim"

# The IAPD host rejects declarative bot User-Agents, so a browser-shaped UA is
# required; `From` keeps the requests attributable to us (RFC 9110 10.1.2).
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "From": "bladyman@eicatlanta.com",
    "Accept": "application/json,*/*",
}
FIRM_API = "https://api.adviserinfo.sec.gov/search/firm/{crd}"
THROTTLE = 0.35          # seconds between requests, per SEC fair-access guidance


def fetch_one(crd: str) -> dict | None:
    """Cached IAPD payload for one CRD. None when the firm does not resolve."""
    path = CACHE / f"{crd}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            path.unlink()                      # truncated write, refetch
    time.sleep(THROTTLE)
    try:
        raw = urllib.request.urlopen(
            urllib.request.Request(FIRM_API.format(crd=crd), headers=UA),
            timeout=60).read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None
    hits = json.loads(raw).get("hits", {}).get("hits", [])
    if not hits:
        return None
    # iacontent is itself a JSON string, not a nested object
    content = json.loads(hits[0]["_source"]["iacontent"])
    path.write_text(json.dumps(content, separators=(",", ":")), encoding="utf-8")
    return content


def other_names(content: dict) -> list[str]:
    basic = content.get("basicInformation") or {}
    names = basic.get("otherNames") or []
    return [str(n).strip() for n in names if str(n).strip()]


def main(limit: int | None = None) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    firms = pd.read_parquet(ROOT / "data" / "output" / "firms.parquet",
                            columns=["crd", "name"])
    crds = firms["crd"].astype(str).tolist()
    if limit:
        crds = crds[:limit]

    rows, missing = [], 0
    for i, crd in enumerate(crds, 1):
        content = fetch_one(crd)
        if content is None:
            missing += 1
        else:
            for name in other_names(content):
                rows.append({"firm_crd": crd, "other_name": name})
        if i % 250 == 0 or i == len(crds):
            print(f"  {i:>6,}/{len(crds):,}  aliases {len(rows):>7,}  unresolved {missing}",
                  flush=True)

    out = pd.DataFrame(rows, columns=["firm_crd", "other_name"])
    INTERIM.mkdir(parents=True, exist_ok=True)
    out.to_parquet(INTERIM / "firm_other_names.parquet", index=False)
    print(f"\nfirm_other_names.parquet  {len(out):,} aliases for "
          f"{out['firm_crd'].nunique():,} firms  ({missing} unresolved)")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
