"""Rebuild every state layer plus the national map/search metadata."""
from __future__ import annotations

import pathlib
import re

from export_geojson import export
from export_national import main as export_national
from export_firm_profiles import main as export_firm_profiles
from export_advisor_history import main as export_advisor_history
from export_barrons import main as export_barrons
from export_forbes import main as export_forbes
from validate_webapp_data import main as validate_webapp


ROOT = pathlib.Path(__file__).parents[1]
INTERIM = ROOT / "data" / "interim"
STATE_FILE = re.compile(r"branch_geocoded_([A-Z]{2})\.parquet$")


def main() -> None:
    states = sorted(
        match.group(1)
        for path in INTERIM.glob("branch_geocoded_*.parquet")
        if (match := STATE_FILE.match(path.name))
    )
    if not states:
        raise SystemExit(f"No branch_geocoded state files found under {INTERIM}")
    print(f"Rebuilding {len(states)} state layers: {' '.join(states)}")
    for state in states:
        export(state)
    export_national()
    export_firm_profiles()
    export_advisor_history()

    # Barron's comes from a browser harvest, not the SEC feeds, so it can be
    # absent on a clean checkout. A missing ranking file must not stop a
    # rebuild of the data the map actually needs to draw.
    try:
        export_barrons()
    except SystemExit as exc:
        print(f"Skipping Barron's rankings: {exc}")
    try:
        export_forbes()
    except SystemExit as exc:
        print(f"Skipping Forbes rankings: {exc}")

    # The gate runs as part of the build, not as a file someone might remember
    # to execute. It had been failing on every rebuild since de-duplication
    # landed and nothing surfaced it, because no pipeline script called it.
    print()
    print("Validating generated artifacts...")
    validate_webapp()


if __name__ == "__main__":
    main()
