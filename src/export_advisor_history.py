"""Per-advisor employment history for the map's advisor detail panel.

Two facts the pin data cannot carry, because both are per-advisor rather than
per-advisor-branch:

  * when the advisor joined their CURRENT firm -- advisor_employments
    .reg_earliest_date, which is 100% populated and agrees with the employment
    history to the month (CRD 2857657: 2009-06-01 vs "06/2009").
  * every firm they were registered with BEFORE -- advisor_prior_registrations,
    which carries firm_crd on 100% of its rows. That is what makes this worth
    exporting rather than the bare count already in the pins: each prior firm
    links straight into the firm overview the webapp already has, so "left a
    competitor for a wirehouse" becomes visible instead of being the number 7.
  * slower-changing experience and state-registration detail. These remain
    available on the advisor profile without inflating every hot map pin.

Sharded by the last two digits of the advisor CRD into 100 files. As one
artifact this is 31 MB, which is far too much to fetch when a salesperson opens
a single advisor; sharded, opening an advisor costs one ~320 KB file, and the
shards a rep touches in a session stay cached. The bucket is derivable from the
CRD alone, so the webapp needs no index to find the right file.

Compact format, per shard:
  {advisor_crd: [joined_iso, prior_rows, years_experience, current_rows]}
  current_rows are [[firm_crd, joined_iso, n_registrations, reg_states], ...].
  joined_iso may be null; the prior list may be empty. Firm names are repeated
  rather than interned -- interning saves little once the data is split, and
  per-shard dictionaries would have to be duplicated across all 100 files.
"""
from __future__ import annotations

import json
import pathlib

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
INTERIM = ROOT / "data" / "interim"
OUTPUT = ROOT / "data" / "output"
WEB = ROOT / "webapp" / "data"

# An advisor with 40 prior registrations is a data artefact more than a career;
# keep the most recent and let IAPD hold the tail. Measured max is far below
# this for the overwhelming majority, so it changes almost nothing.
MAX_PRIOR = 15


def _iso(value) -> str | None:
    """Dates arrive as strings or timestamps depending on the feed."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if pd.isna(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "nat"}:
        return None
    return text[:10]


def shard_key(advisor_crd: str) -> str:
    """Last two digits of the CRD, zero-padded. Must match the webapp's rule."""
    return str(advisor_crd).strip()[-2:].zfill(2)


def _title(name: str) -> str:
    """Feed names are shouted; the panel is not."""
    text = str(name).strip()
    return text.title() if text.isupper() else text


def main() -> None:
    WEB.mkdir(parents=True, exist_ok=True)

    # Only advisors the map can actually show. Exporting history for advisors
    # with no mapped branch would inflate the file with rows nothing can open.
    branches = pd.read_parquet(
        INTERIM / "advisor_branches.parquet", columns=["advisor_crd"]
    )
    mapped = set(branches["advisor_crd"].astype(str))

    employments = pd.read_parquet(
        INTERIM / "advisor_employments.parquet",
        columns=["advisor_crd", "firm_crd", "reg_earliest_date",
                 "n_registrations", "reg_states"],
    )
    employments["advisor_crd"] = employments["advisor_crd"].astype(str)
    employments = employments[employments["advisor_crd"].isin(mapped)]
    # An advisor registered with several affiliates of one group has a row per
    # affiliate. The earliest is when they joined the group, which is the date
    # a salesperson means by "how long have they been there".
    joined = (
        employments.assign(d=employments["reg_earliest_date"].map(_iso))
        .dropna(subset=["d"])
        .groupby("advisor_crd")["d"]
        .min()
    )
    current: dict[str, list] = {}
    for advisor_crd, group in employments.groupby("advisor_crd", sort=False):
        current[advisor_crd] = [
            [str(row.firm_crd), _iso(row.reg_earliest_date),
             None if pd.isna(row.n_registrations) else int(row.n_registrations),
             None if pd.isna(row.reg_states) else str(row.reg_states)]
            for row in group.itertuples(index=False)
        ]

    experience_frame = pd.read_parquet(
        OUTPUT / "advisor_experience.parquet",
        columns=["advisor_crd", "years_experience"],
    )
    experience_frame["advisor_crd"] = experience_frame["advisor_crd"].astype(str)
    experience = {
        row.advisor_crd: (None if pd.isna(row.years_experience)
                          else round(float(row.years_experience), 1))
        for row in experience_frame.itertuples(index=False)
        if row.advisor_crd in mapped
    }

    prior = pd.read_parquet(
        INTERIM / "advisor_prior_registrations.parquet",
        columns=["advisor_crd", "firm_crd", "firm_name_on_record",
                 "reg_begin", "reg_end"],
    )
    prior["advisor_crd"] = prior["advisor_crd"].astype(str)
    prior = prior[prior["advisor_crd"].isin(mapped)]
    prior["begin"] = prior["reg_begin"].map(_iso)
    prior["end"] = prior["reg_end"].map(_iso)
    # most recently left first -- the last firm is the one a rep asks about
    prior = prior.sort_values("end", ascending=False, na_position="first")

    records: dict[str, list] = {}
    for advisor_crd, group in prior.groupby("advisor_crd", sort=False):
        rows = [
            [str(row.firm_crd), _title(row.firm_name_on_record), row.begin, row.end]
            for row in group.head(MAX_PRIOR).itertuples(index=False)
        ]
        records[advisor_crd] = [joined.get(advisor_crd), rows,
                                experience.get(advisor_crd), current.get(advisor_crd, [])]

    # advisors with a join date but no prior registrations still get an entry
    for advisor_crd, date in joined.items():
        records.setdefault(advisor_crd, [date, [], experience.get(advisor_crd),
                                         current.get(advisor_crd, [])])

    out_dir = WEB / "history"
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in out_dir.glob("*.json"):
        path.unlink()

    shards: dict[str, dict[str, list]] = {f"{i:02d}": {} for i in range(100)}
    for advisor_crd, value in records.items():
        shards[shard_key(advisor_crd)][advisor_crd] = value

    total = 0
    for bucket, payload in shards.items():
        path = out_dir / f"{bucket}.json"
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        total += path.stat().st_size

    sizes = sorted(p.stat().st_size for p in out_dir.glob("*.json"))
    with_prior = sum(1 for value in records.values() if value[1])
    print(f"webapp/data/history/  {len(shards)} shards, {total / 1024 / 1024:.1f} MB total")
    print(f"  per shard: median {sizes[len(sizes) // 2] / 1024:.0f} KB, "
          f"max {sizes[-1] / 1024:.0f} KB")
    print(f"  {len(records):,} advisors  ({with_prior:,} with prior registrations)")


if __name__ == "__main__":
    main()
