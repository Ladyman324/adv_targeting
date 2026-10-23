"""Download the IAPD compilation feeds (firm + individual) into data/raw/.

These are the full Form ADV compilations. The CSV roster the project started
from is, in the SEC's own words, "some, but not all" of what firms file.
Filenames are date-stamped and regenerate, so the current date is constructed
rather than hardcoded.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import gzip
import tempfile
import zipfile
import xml.etree.ElementTree as ET
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


def validate_feed(path: pathlib.Path, *, individual: bool = False) -> int:
    """Fully consume compressed XML, validating CRC/truncation and record type."""
    def count(stream):
        records = 0
        for _, element in ET.iterparse(stream, events=("end",)):
            if element.tag.rsplit("}", 1)[-1] == ("Indvl" if individual else "Firm"):
                records += 1
                element.clear()
        return records
    if individual:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip():
                raise ValueError("SEC ZIP checksum failed")
            members = [name for name in archive.namelist() if name.lower().endswith(".xml")]
            if not members:
                raise ValueError("SEC ZIP contains no XML")
            records = 0
            for name in members:
                with archive.open(name) as stream:
                    records += count(stream)
    else:
        with gzip.open(path, "rb") as stream:
            records = count(stream)
    if not records:
        raise ValueError("SEC feed has no expected records")
    return records


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
            validate_feed(dest, individual=key == "individual")
            print(f"  cached  {fname}  ({dest.stat().st_size/1e6:.1f} MB)")
            return dest
        url = f"{BASE}/{fname}"
        try:
            req = urllib.request.Request(url, headers=UA)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=RAW, prefix=fname + ".", suffix=".part", delete=False) as f:
                    temporary = pathlib.Path(f.name)
                    with urllib.request.urlopen(req, timeout=900) as r:
                        total, got = int(r.headers.get("Content-Length", 0)), 0
                        while chunk := r.read(1 << 20):
                            f.write(chunk)
                            got += len(chunk)
                if total and total != got:
                    raise ValueError(f"Incomplete SEC download: expected {total}, got {got}")
                validate_feed(temporary, individual=key == "individual")
                temporary.replace(dest)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
            print(f"  fetched {fname}  ({got/1e6:.1f} MB)  [{label}]")
            return dest
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    raise FileNotFoundError(f"no {key} feed found in the last {lookback} days")


if __name__ == "__main__":
    for key in (sys.argv[1:] or ["firm_sec", "individual"]):
        download(key)
