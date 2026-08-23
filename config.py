"""Paths and tunable thresholds for the ADV targeting pipeline."""
import os
import re
from pathlib import Path

# Either the SEC download ZIP or the extracted firm-roster CSV — loader handles both.
# ADV_SOURCE overrides it from the environment, so a second machine (or a rerun
# against a newer quarter) does not require editing this file.
ADV_SOURCE = Path(os.environ.get("ADV_SOURCE") or (
    r"C:\Users\bladyman.eicatlanta\Downloads\ia07012026"
    r"\IA_SEC_-_FIRM_ROSTER_FOIA_DOWNLOAD_-_34622660.CSV"
))

ROOT     = Path(__file__).parent
INTERIM  = ROOT / "data" / "interim"
OUTPUT   = ROOT / "data" / "output"


def newest_feed(directory: Path, pattern: str) -> Path:
    """The most recent SEC feed matching `pattern`, chosen explicitly.

    Callers used `next(dir.glob(...))`, which takes whatever the filesystem
    happens to return first. With one feed present that is correct by accident;
    the moment a second quarter is downloaded it silently becomes a coin flip,
    and a build that mixes an old advisor feed with a new firm roster looks
    completely normal.

    The feeds are date-stamped (IA_INDVL_Feed_07_01_2026.xml.zip), so the date
    in the name is the sort key -- not mtime, which a copy or a sync resets.
    """
    found = sorted(directory.glob(pattern))
    if not found:
        raise FileNotFoundError(
            f"No file matching {pattern!r} in {directory}. Download the SEC feed first.")

    def stamp(p: Path) -> tuple:
        m = re.search(r"(\d{2})[_-](\d{2})[_-](\d{4})", p.name)
        return (int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else (0, 0, 0)

    found.sort(key=stamp)
    chosen = found[-1]
    if len(found) > 1 and stamp(chosen) == (0, 0, 0):
        raise ValueError(
            f"{len(found)} files match {pattern!r} in {directory} and none carry a "
            f"readable date, so there is no non-arbitrary way to choose: "
            f"{[p.name for p in found]}. Move the stale ones out.")
    if len(found) > 1:
        print(f"[*] {pattern}: {len(found)} present, using the newest -- {chosen.name}")
    return chosen

# Equity Investment Corporation — us. Used as the pipeline's self-test.
OUR_CRD = "283930"

# --- Tunable thresholds -------------------------------------------------
MIN_RAUM        = 150_000_000     # below this, can't clear SMA minimums at scale
SMA_MIN_ACCOUNT = 400_000         # avg account size needed for a value SMA to be viable
PRODUCT_FIT_MIN = 45              # transparent initial heuristic; recalibrate from sales outcomes

# Two conditions, because either alone misfires. Share of RAUM catches
# institutional-only managers. The client count catches family offices and
# institutional consultants -- a firm with 14 clients and $6.7B is one
# relationship, not a distribution channel, however retail those clients are.
MIN_RETAIL_RAUM_SHARE = 0.05
MIN_RETAIL_CLIENTS = 100

# Wrap-portfolio-manager assets as a share of RAUM, above which the firm is a
# manager rather than a distributor. EIC sits at 78%; Mariner and Corient at 0.06%.
WRAP_PM_MATERIAL = 0.10

# Firms above this are NOT excluded — RIA aggregators (Captrust, Creative Planning,
# Edelman) are among the largest buyers of outside managers. They route to the
# gatekeeper tier instead, because reaching them is a shelf-space conversation
# rather than a field-sales one.
GATEKEEPER_RAUM = 10_000_000_000
