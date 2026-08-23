"""Forbes rankings -> webapp/data/forbes.json, keyed on advisor CRD.

Two jobs:

  1. Ship the Forbes rankings for advisors we can identify with confidence.
  2. Backfill TEAM ASSETS, which Barron's has for nobody. The Barron's harvest
     came back with an empty detail panel on all 1,788 rankings, so book size
     -- the best single proxy for whether an advisor is worth a call -- was
     missing entirely. Forbes publishes it for 100% of its rows, and any
     advisor on both lists inherits it.

ONLY "confirmed" and "high" matches are exported. The "review" tier stays in
data/interim/forbes_matches.parquet for eyeballing and never reaches the map:
a wrong Forbes badge is worse than no badge, because a rep who congratulates
the wrong person on a ranking has spent credibility to gain nothing.

Provenance travels with every row. `t` records how the CRD was established:
    c  confirmed -- CRD from the Barron's BrokerCheck link, not inferred
    h  high      -- inferred by src/forbes_match.py, above threshold, unambiguous
The distinction has to survive into the UI. Two badges that look identical but
rest on an exact join and a probabilistic one are the thing to avoid.
"""
from __future__ import annotations

import json
import pathlib

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
INTERIM = ROOT / "data" / "interim"
OUT = ROOT / "webapp" / "data"

TIER_CODE = {"confirmed": "c", "high": "h"}

SEGMENT_SHORT = {"High Net Worth": "HNW", "Private Wealth": "PW"}


def short_label(row, rank) -> str:
    """The compact badge text.

    Labels are built HERE, not in the client, for one specific reason: the
    national list carries no category, so parse_forbes.py inherits a state for
    it from the same person's best-in-state row purely to make matching work.
    That state is a matching aid, not a rank scope -- rebuilding a label from
    it client-side would print a national #1 as "#1 IN GA", which is false.
    """
    if rank is None:
        return "RANKED"
    if not isinstance(row.category, str):          # national list
        return f"TOP WEALTH #{rank}"
    market = row.rank_market
    if isinstance(market, str) and market:
        segment = SEGMENT_SHORT.get(row.rank_segment or "", "")
        return f"#{rank} {market.upper()}{f' ({segment})' if segment else ''}"
    state = row.rank_state if isinstance(row.rank_state, str) else None
    return f"#{rank} IN {state}" if state else f"#{rank}"


def full_label(row) -> str:
    if not isinstance(row.category, str):
        return "Forbes Top Wealth Advisors (national)"
    return f"Forbes Best-In-State Wealth Advisors — {row.category}"


def main() -> None:
    source = INTERIM / "forbes_matches.parquet"
    if not source.exists():
        raise SystemExit(f"{source} not found. Run src/forbes_match.py first.")

    matched = pd.read_parquet(source)
    shipped = matched[matched["match_tier"].isin(TIER_CODE)
                      & matched["advisor_crd"].notna()].copy()
    shipped["advisor_crd"] = shipped["advisor_crd"].astype(str)

    # Restrict to advisors the map can actually show. advisors.parquet holds
    # 436k people but only those with a mapped branch reach the webapp, so a
    # badge for anyone else is unreachable -- and the release gate rejects a
    # forbes.json naming a CRD the search index does not contain.
    index = json.loads((OUT / "advisor_index.json").read_text(encoding="utf-8"))
    mapped = {str(row[0]) for row in index["advisors"]}
    before = len(shipped)
    shipped = shipped[shipped["advisor_crd"].isin(mapped)]
    unmapped = before - len(shipped)

    # Rank ascending within an advisor so entry 0 is their best placement.
    shipped = shipped.sort_values(["advisor_crd", "rank"])

    advisors: dict[str, list] = {}
    assets: dict[str, float] = {}
    for crd, group in shipped.groupby("advisor_crd", sort=False):
        entries = []
        for row in group.itertuples():
            rank = None if pd.isna(row.rank) else int(row.rank)
            entries.append([
                short_label(row, rank),
                full_label(row),
                rank,
                TIER_CODE[row.match_tier],
                f"https://www.forbes.com/profile/{row.forbes_uri}/",
            ])
            # Team assets describe the TEAM, so the same figure recurs across an
            # advisor's rankings and across teammates. Keep the largest rather
            # than summing, which would multiply one book by its headcount.
            if not pd.isna(row.team_assets_usd):
                assets[crd] = max(assets.get(crd, 0.0), float(row.team_assets_usd))
        advisors[crd] = entries

    payload = {
        "advisors": advisors,
        # separate map: an advisor can have team assets from Forbes while their
        # badge on the map comes from Barron's, and the app reads them apart
        "team_assets": {k: round(v) for k, v in assets.items()},
        "tiers": {"c": "Confirmed via BrokerCheck", "h": "Matched on name, firm, and location"},
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "forbes.json").write_text(json.dumps(payload, separators=(",", ":")),
                                     encoding="utf-8")

    counts = shipped["match_tier"].value_counts()
    print(f"forbes.json  {len(advisors):,} advisors, {len(shipped):,} rankings")
    for tier in ("confirmed", "high"):
        print(f"  {tier:<10}{int(counts.get(tier, 0)):>7,}")
    print(f"  team assets backfilled for {len(assets):,} advisors")

    print(f"  dropped, not a mapped advisor: {unmapped:,}")
    dropped = len(matched) - len(shipped) - unmapped
    print(f"  withheld (review/none): {dropped:,} "
          f"({dropped / max(1, len(matched)) * 100:.1f}%) -- not shipped by design")

    # How much of the Barron's population gains a book size it did not have
    barrons_path = OUT / "barrons.json"
    if barrons_path.exists():
        barrons = set(json.loads(barrons_path.read_text(encoding="utf-8"))["advisors"])
        gained = len(barrons & set(assets))
        print(f"  Barron's advisors gaining team assets: {gained:,} of {len(barrons):,}")


if __name__ == "__main__":
    main()
