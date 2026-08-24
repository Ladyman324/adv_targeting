"""Add ADV opportunity pools and mapped-advisor totals to the national artifact.

This is a fast post-export step for an existing web data build. The full national
export writes the same eight-field firm schema directly.
"""
from __future__ import annotations

from collections import defaultdict
import json
import pathlib

import pandas as pd


ROOT = pathlib.Path(__file__).parents[1]
WEB = ROOT / "webapp" / "data"


def millions(value):
    return None if pd.isna(value) else round(float(value) / 1e6, 1)


def main() -> None:
    facts = pd.read_parquet(
        ROOT / "data" / "output" / "firms.parquet",
        columns=[
            "crd", "raum_equity_exchange_implied",
            "raum_fund_shares_ric_implied",
        ],
    ).set_index("crd")
    facts.index = facts.index.astype(str)

    advisors: dict[str, set[str]] = defaultdict(set)
    for path in sorted(WEB.glob("pins_??.json")):
        layer = json.loads(path.read_text(encoding="utf-8"))
        crds = [str(firm[7]) for firm in layer["firms"]]
        for pin in layer["pins"]:
            advisors[crds[pin[2]]].add(str(pin[6]))

    path = WEB / "offices_national.json"
    national = json.loads(path.read_text(encoding="utf-8"))
    enriched = []
    for firm in national["firms"]:
        crd = str(firm[1])
        row = facts.loc[crd]
        enriched.append(firm[:5] + [
            millions(row["raum_equity_exchange_implied"]),
            millions(row["raum_fund_shares_ric_implied"]),
            len(advisors[crd]),
        ])
    national["firms"] = enriched
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(national, separators=(",", ":")), encoding="utf-8")
    temp.replace(path)
    print(f"Enriched {len(enriched):,} firms; mapped-advisor range "
          f"{min(map(len, advisors.values())):,}–{max(map(len, advisors.values())):,}.")
    write_national_view(national)


# The national map's opening payload.
#
# offices_national.json is 1.45 MB compressed and the browser spends 7.1 SECONDS
# on the wire fetching it before the map can draw -- measured, and it is
# transfer alone: parsing those 125,183 offices takes 12ms and building both
# index maps from them takes 4.6ms. So the fix is fewer bytes, and only fewer
# bytes.
#
# At national zoom two offices a quarter-degree apart cannot be told apart, so
# the offices collapse to a GRID of counts. The first-paint artifact carries no
# firm dictionary: offices_national.json is the authoritative detail file and
# already contains it. Shipping the same firms twice made almost all of the
# opening payload irrelevant to the first paint.
#
# A cell stays separate by STATE, even when two states round to the same
# quarter-degree coordinate. That lets the browser apply Continental U.S.
# exactly rather than infer a jurisdiction from latitude/longitude. It also
# carries two counts: every placement and placements at firms reporting ADV
# Item 5.G(7). The latter matters because that filter is on by default; the old
# grid displayed every firm until office detail arrived while claiming the
# selecting-managers filter was active.
#
# The full office array is still written, and still fetched -- in the
# background, after the map is already usable, so the national layer upgrades
# from grid to individual buildings a few seconds in. Nothing is lost; the
# detail simply stops being a precondition for seeing anything.
CELL = 0.25


def build_national_view(national: dict) -> dict:
    """Return the compact, filter-honest first-paint national payload.

    Grid rows are ``[lat, lon, all_count, selecting_count, state_index]``.
    ``selecting_count`` includes only offices whose firm reports ADV Item
    5.G(7), the application's default national targeting filter.
    """
    grid: dict[tuple[int, float, float], list[int]] = defaultdict(
        lambda: [0, 0]
    )
    firms = national["firms"]
    for office in national["offices"]:
        lon, lat, count, firm_index = office[:4]
        state_index = office[6]
        cell_lat = round(round(lat / CELL) * CELL, 3)
        cell_lon = round(round(lon / CELL) * CELL, 3)
        totals = grid[(state_index, cell_lat, cell_lon)]
        totals[0] += count
        if firms[firm_index][4]:
            totals[1] += count

    rows = [
        [lat, lon, counts[0], counts[1], state_index]
        for (state_index, lat, lon), counts in sorted(grid.items())
    ]
    return {"states": list(national["states"]), "grid": rows}


def write_national_view(national: dict) -> None:
    view = build_national_view(national)
    out = WEB / "national_view.json"
    temp = out.with_suffix(".json.tmp")
    temp.write_text(json.dumps(view, separators=(",", ":")), encoding="utf-8")
    temp.replace(out)
    print(f"national_view.json: {len(view['grid']):,} grid cells "
          f"({out.stat().st_size / 1024:.0f} KB raw) from "
          f"{len(national['offices']):,} offices")


if __name__ == "__main__":
    main()
