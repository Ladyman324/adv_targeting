"""Download the IAPD compilation feeds (firm + individual) into data/raw/.

These are the full Form ADV compilations. The CSV roster the project started
from is, in the SEC's own words, "some, but not all" of what firms file.
Filenames are date-stamped and regenerate, so the current date is constructed
rather than hardcoded.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys
import urllib.error
import urllib.request

RAW = pathlib.Path(__file__).parents[1] / "data" / "raw"
BASE = "https://reports.adviserinfo.sec.gov/reports/CompilationReports"

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "From": "bladyman@eicatlanta.com",
}

FEEDS = {
    "firm_sec":  ("IA_FIRM_SEC_Feed_{d}.xml.gz",   "SEC-registered firms"),
    "individual": ("IA_INDVL_Feed_{d}.xml.zip",    "Investment adviser representatives"),
    "firm_state": ("IA_FIRM_STATE_Feed_{d}.xml.gz", "State-registered firms"),
}


def _stamp(day: dt.date) -> str:
    return day.strftime("%m_%d_%Y")


def download(key: str, day: dt.date | None = None, lookback: int = 7) -> pathlib.Path:
    """Fetch a feed, walking back day by day until a published stamp is found."""
    pattern, label = FEEDS[key]
    day = day or dt.date.today()
    RAW.mkdir(parents=True, exist_ok=True)

    for back in range(lookback):
        d = day - dt.timedelta(days=back)
        fname = pattern.format(d=_stamp(d))
        dest = RAW / fname
        if dest.exists():
            print(f"  cached  {fname}  ({dest.stat().st_size/1e6:.1f} MB)")
            return dest
        url = f"{BASE}/{fname}"
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=900) as r, open(dest, "wb") as f:
                total, got = int(r.headers.get("Content-Length", 0)), 0
                while chunk := r.read(1 << 20):
                    f.write(chunk); got += len(chunk)
            print(f"  fetched {fname}  ({got/1e6:.1f} MB)  [{label}]")
            return dest
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            dest.unlink(missing_ok=True)
    raise FileNotFoundError(f"no {key} feed found in the last {lookback} days")


if __name__ == "__main__":
    for key in (sys.argv[1:] or ["firm_sec", "individual"]):
        download(key)
