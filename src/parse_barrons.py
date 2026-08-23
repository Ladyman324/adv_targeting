"""Ingest the Barron's ranking harvest and join it to our advisor universe.

Input is barrons_rankings.json, produced by src/barrons_harvest.js running in a
signed-in browser (the rankings are paywalled and client-rendered, so they
cannot be fetched server-side).

The join key is the advisor CRD, taken from the BrokerCheck link on each
Barron's advisor page. That is the same identifier the SEC feeds use, so a
ranked advisor lands directly on the person already on the map.

Four rankings, and they do not mean the same thing:
  top1500      ranked WITHIN a state -- rank 1 is the top advisor in that state,
               so rank is only comparable against others in the same state
  women        national rank, with team assets
  independent  national rank, with team assets
  top100       national rank, with team assets

Writes data/interim/barrons_rankings.parquet, one row per advisor-ranking.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
INTERIM = ROOT / "data" / "interim"

# label as Barron's writes it -> our column, with the unit folded into the name
DETAIL_FIELDS = {
    "team assets": ("team_assets_bil", 1e9),
    "typical account": ("typical_account_mil", 1e6),
    "typical net worth": ("typical_net_worth_mil", 1e6),
}


def _number(value):
    """'290.9' or '$1.2' or '1,250' -> float. Barron's mixes formats."""
    if value is None:
        return None
    text = re.sub(r"[^0-9.]", "", str(value))
    try:
        return float(text) if text else None
    except ValueError:
        return None


def _detail(row: dict) -> dict:
    """Flatten the per-row detail panel. Labels carry their own units --
    'Team Assets ($bil)' -- so match on the stem and keep the scale explicit."""
    out = {}
    for label, value in (row.get("detail") or {}).items():
        stem = re.sub(r"\(.*?\)", "", str(label)).strip().lower()
        for key, (column, scale) in DETAIL_FIELDS.items():
            if stem.startswith(key):
                number = _number(value)
                out[column] = number
                out[column.rsplit("_", 1)[0] + "_usd"] = (
                    None if number is None else number * scale)
        if stem.startswith("client type"):
            out["client_types"] = str(value).strip()
        if re.match(r"^\d{4} rank$", stem):
            out["prior_rank"] = _number(value)
    return out


def load(path: pathlib.Path) -> pd.DataFrame:
    rows = json.loads(path.read_text(encoding="utf-8"))
    records = []
    for row in rows:
        record = {
            "ranking": row.get("ranking"),
            "year": row.get("year"),
            "rank_state": (row.get("state") or None),
            "rank": row.get("rank"),
            "advisor_name": row.get("advisor"),
            "firm_name_barrons": row.get("firm"),
            "location": row.get("location"),
            "advisor_crd": (str(row["crd"]).strip() if row.get("crd") else None),
            "barrons_url": row.get("barrons_url"),
        }
        record.update(_detail(row))
        records.append(record)
    table = pd.DataFrame(records)
    # rank scope is not the same across the four lists; make it explicit rather
    # than leaving a bare "rank" column that invites cross-list comparison
    table["rank_scope"] = table["ranking"].map(
        lambda r: "state" if r == "top1500" else "national")
    return table


def main(path: str | None = None) -> None:
    source = pathlib.Path(path) if path else (ROOT / "data" / "raw" / "barrons_rankings.json")
    if not source.exists():
        raise SystemExit(
            f"{source} not found.\n"
            "Run src/barrons_harvest.js in a signed-in barrons.com console, then move\n"
            "the downloaded barrons_rankings.json to data/raw/.")

    table = load(source)
    INTERIM.mkdir(parents=True, exist_ok=True)
    table.to_parquet(INTERIM / "barrons_rankings.parquet", index=False)

    print(f"barrons_rankings.parquet  {len(table):,} rows")
    for name, group in table.groupby("ranking"):
        got = group["advisor_crd"].notna().sum()
        print(f"  {name:<12}{len(group):>6,} rows  {got:>6,} with a CRD "
              f"({got / max(1, len(group)) * 100:5.1f}%)")

    # how much of this lands on somebody we already map
    branches = ROOT / "data" / "output" / "advisor_branches.parquet"
    if not branches.exists():
        print("\n(advisor_branches.parquet missing -- skipping the join report)")
        return
    mapped = set(pd.read_parquet(branches, columns=["advisor_crd"])["advisor_crd"].astype(str))
    known = table["advisor_crd"].dropna().astype(str)
    hits = known[known.isin(mapped)]
    print(f"\nCRDs present: {known.nunique():,} distinct advisors")
    print(f"  matched to a mapped advisor: {hits.nunique():,} "
          f"({hits.nunique() / max(1, known.nunique()) * 100:.1f}%)")
    print("  unmatched are expected: Barron's ranks brokers and bank advisors "
          "whose firms never file Form ADV as an RIA.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
