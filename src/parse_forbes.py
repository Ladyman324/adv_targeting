"""Ingest the Forbes wealth-advisor harvest.

Input is forbes_rankings.json from src/forbes_harvest.js.

Forbes carries NO CRD -- not in the list payload, not on the advisor profile
pages, which contain zero FINRA references. That makes this fundamentally
different from Barron's, where a BrokerCheck link gave an exact join for free.
Everything here is therefore keyed on a synthetic id, and the join to our
advisor universe is a separate, probabilistic step in src/forbes_match.py.

Ranking scope is messier than Barron's too. The category field holds 112
distinct values, not 51 states:

    "Georgia - Atlanta (High Net Worth)"
    "Georgia - Atlanta (Private Wealth)"
    "Georgia - State"

Those are three separate rankings that each start at 1, so a rank is only
meaningful next to its full category. We split the category into state /
market / segment but keep the original verbatim as well, so that a change in
Forbes' wording is visible rather than silently mis-parsed.

Writes data/interim/forbes_rankings.parquet.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
INTERIM = ROOT / "data" / "interim"

STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

# "$5.7B" / "$966M" / "$133B" -> dollars. Forbes mixes both suffixes freely.
_MONEY = re.compile(r"\$?\s*([\d.,]+)\s*([BMK])?", re.I)
_SCALE = {"b": 1e9, "m": 1e6, "k": 1e3}


def money(value) -> float | None:
    if not value:
        return None
    match = _MONEY.search(str(value))
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return number * _SCALE.get((match.group(2) or "").lower(), 1.0)


def split_category(category: str | None) -> tuple[str | None, str | None, str | None]:
    """'Georgia - Atlanta (High Net Worth)' -> ('GA', 'Atlanta', 'High Net Worth').

    'Georgia - State' -> ('GA', None, None). Anything unrecognised keeps a null
    state rather than guessing, so a bad parse shows up as missing data instead
    of a plausible-looking wrong answer.
    """
    if not category:
        return None, None, None
    segment = None
    text = category
    bracket = re.search(r"\(([^)]*)\)", text)
    if bracket:
        segment = bracket.group(1).strip()
        text = text[: bracket.start()].strip()
    head, _, tail = text.partition(" - ")
    state = STATE_ABBR.get(head.strip().lower())
    market = tail.strip() or None
    if market and market.lower() == "state":
        market = None
    return state, market, segment


def load(path: pathlib.Path) -> pd.DataFrame:
    rows = json.loads(path.read_text(encoding="utf-8"))
    records = []
    for row in rows:
        state, market, segment = split_category(row.get("category"))
        records.append({
            "source_list": row.get("list"),
            "rank": row.get("rank"),
            "category": row.get("category"),
            "rank_state": state,
            "rank_market": market,
            "rank_segment": segment,
            "advisor_name": row.get("name"),
            "firm_name_forbes": row.get("firm"),
            "city": row.get("city"),
            "forbes_uri": row.get("uri"),
            "forbes_url": f"https://www.forbes.com/profile/{row.get('uri')}/"
                          if row.get("uri") else None,
            "team_assets_usd": money(row.get("teamAssets")),
            "min_account_usd": money(row.get("minAccountSize")),
            "typical_net_worth_raw": row.get("typicalNetWorth"),
            "typical_account_raw": row.get("typicalSize"),
        })
    table = pd.DataFrame(records)
    # the rank scope is the full category, never the bare state -- three GA
    # rankings each have a #1 and they are not the same achievement
    table["rank_scope"] = table["category"]

    # The national list carries no category at all, so it parses to no state --
    # and forbes_match.py gates candidates on (surname, state), meaning every
    # national row would silently match nobody. The same person has the same
    # `uri` on both lists, so the state is inherited from their best-in-state
    # placement. In the 2026 harvest that covers 253 of 253 national rows;
    # anything left stateless is reported rather than quietly dropped.
    known_state = (table.dropna(subset=["rank_state"])
                        .drop_duplicates("forbes_uri")
                        .set_index("forbes_uri")["rank_state"])
    missing = table["rank_state"].isna() & table["forbes_uri"].notna()
    table.loc[missing, "rank_state"] = table.loc[missing, "forbes_uri"].map(known_state)
    table["state_inherited"] = missing & table["rank_state"].notna()
    return table


def main(path: str | None = None) -> None:
    source = (pathlib.Path(path) if path
              else ROOT / "data" / "raw" / "forbes_rankings.json")
    if not source.exists():
        raise SystemExit(
            f"{source} not found.\n"
            "Open https://www.forbes.com/lists/best-in-state-wealth-advisors/, run\n"
            "src/forbes_harvest.js in the console, and move the download to data/raw/.")

    table = load(source)
    INTERIM.mkdir(parents=True, exist_ok=True)
    table.to_parquet(INTERIM / "forbes_rankings.parquet", index=False)

    assets = table["team_assets_usd"].notna().sum()
    print(f"forbes_rankings.parquet  {len(table):,} rows")
    print(f"  team assets present : {assets:,} ({assets / max(1, len(table)) * 100:.1f}%)")
    print(f"  distinct categories : {table['category'].nunique():,}")
    print(f"  states resolved     : {table['rank_state'].notna().sum():,}"
          f"  ({int(table['state_inherited'].sum()):,} inherited via uri)")
    stateless = int(table["rank_state"].isna().sum())
    if stateless:
        print(f"  NO STATE            : {stateless:,} rows -- these cannot be matched")
    unresolved = table.loc[table["rank_state"].isna(), "category"].dropna().unique()
    if len(unresolved):
        print(f"  UNPARSED categories : {len(unresolved)} -> {list(unresolved)[:5]}")
    for name, group in table.groupby("source_list"):
        print(f"  {name:<16}{len(group):>7,}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
