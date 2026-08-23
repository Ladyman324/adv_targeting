"""Barron's rankings -> webapp/data/barrons.json, keyed on advisor CRD.

Deliberately a side lookup rather than another field on the pin arrays. Only
1,522 advisors are ranked out of ~397k mapped, so widening a 403,606-row
positional array to carry it would be ~402,000 nulls -- and that array has
already been broken once by an insertion. This file loads once and joins in
the client on advisor CRD.

The four lists do not mean the same thing and must not be compared to each
other:

    top1500      rank WITHIN a state -- "#5 in GA". Rank 1 recurs 51 times.
    top100       national, all advisors
    women        national, women advisors
    independent  national, independent advisors -- and it is a YEAR OLDER

So a rank is only meaningful alongside the list it came from. The client
renders "#5 IN GA" for top1500 but "TOP 100 #1" for the national lists, and
never just "#1".

234 advisors appear on more than one list, so `lists` is always an array,
ordered by scarcity -- the first entry is the one worth putting on a badge.
Being #1 of the Top 100 should not be displayed as a state rank.
"""
from __future__ import annotations

import json
import pathlib

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
INTERIM = ROOT / "data" / "interim"
OUT = ROOT / "webapp" / "data"

# Scarcity order: the rarer the list, the better the badge. Top 100 is 100 of
# ~397k; a state rank is one of ~29 in that state.
LIST_ORDER = ["top100", "independent", "women", "top1500"]
LIST_RANK = {name: i for i, name in enumerate(LIST_ORDER)}


def main() -> None:
    source = INTERIM / "barrons_rankings.parquet"
    if not source.exists():
        raise SystemExit(
            f"{source} not found. Run src/parse_barrons.py first.")

    table = pd.read_parquet(source)
    table = table[table["advisor_crd"].notna()].copy()
    table["advisor_crd"] = table["advisor_crd"].astype(str).str.strip()
    table["order"] = table["ranking"].map(LIST_RANK).fillna(len(LIST_ORDER))
    table = table.sort_values(["advisor_crd", "order", "rank"])

    advisors: dict[str, list] = {}
    for crd, group in table.groupby("advisor_crd", sort=False):
        entries = []
        for row in group.itertuples():
            entries.append([
                row.ranking,
                None if pd.isna(row.rank) else int(row.rank),
                row.rank_state if isinstance(row.rank_state, str) else None,
                None if pd.isna(row.year) else int(row.year),
                row.barrons_url or "",
            ])
        advisors[crd] = entries

    payload = {
        # the client needs the label text; keeping it here means the wording is
        # fixed in one place rather than duplicated across three render paths
        "labels": {
            # named for what the data is -- a state-by-state ranking -- rather
            # than for Barron's cover title, which we have not verified
            "top1500": "Top Financial Advisors, by state",
            "top100": "Top 100 Financial Advisors",
            "women": "Top 100 Women Financial Advisors",
            "independent": "Top 100 Independent Advisors",
        },
        "order": LIST_ORDER,
        "advisors": advisors,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "barrons.json").write_text(json.dumps(payload, separators=(",", ":")),
                                      encoding="utf-8")

    multi = sum(1 for v in advisors.values() if len(v) > 1)
    print(f"barrons.json  {len(advisors):,} ranked advisors, "
          f"{len(table):,} rankings, {multi:,} on more than one list")
    for name in LIST_ORDER:
        part = table[table["ranking"] == name]
        if len(part):
            print(f"  {name:<12}{len(part):>6,}  {int(part['year'].iloc[0])}")


if __name__ == "__main__":
    main()
