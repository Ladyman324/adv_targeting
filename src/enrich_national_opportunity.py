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
# the offices collapse to a GRID of counts: 4,341 cells, 17 KB, visually the
# same map. The firm dictionary rides along because the panel needs firm names
# at every scope, and it is now 93% of what remains.
#
# The full office array is still written, and still fetched -- in the
# background, after the map is already usable, so the national layer upgrades
# from grid to individual buildings a few seconds in. Nothing is lost; the
# detail simply stops being a precondition for seeing anything.
CELL = 0.25


def write_national_view(national: dict) -> None:
    grid: dict[tuple, int] = defaultdict(int)
    for lon, lat, n, *_rest in national["offices"]:
        grid[(round(lat / CELL) * CELL, round(lon / CELL) * CELL)] += n
    view = {
        "firms": national["firms"],
        "states": national["states"],
        # [lat, lon, advisor placements in this cell]
        "grid": [[round(lat, 3), round(lon, 3), n] for (lat, lon), n in grid.items()],
    }
    out = WEB / "national_view.json"
    temp = out.with_suffix(".json.tmp")
    temp.write_text(json.dumps(view, separators=(",", ":")), encoding="utf-8")
    temp.replace(out)
    print(f"national_view.json: {len(view['grid']):,} grid cells "
          f"({out.stat().st_size / 1024:.0f} KB raw) from "
          f"{len(national['offices']):,} offices")


if __name__ == "__main__":
    main()
