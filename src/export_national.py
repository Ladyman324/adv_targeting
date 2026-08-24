"""Build the national map, provenance metadata, and national search index.

The national map contains one record per firm-office combination. A shared
physical address therefore keeps every legal firm and product opportunity instead of
assigning the entire office to its dominant firm.

Compact formats:
  firms   [[display_name, crd, opportunity_score, raum_millions,
            selects_outside_managers, equity_pool_millions,
            fund_etf_pool_millions, mapped_advisors], ...]
  offices [[lon, lat, advisor_placements, firm_idx, motion_code,
            firms_at_physical_office, state_idx, physical_office_id], ...]
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import pathlib
import re

import pandas as pd

from export_geojson import display_firm, apply_placement
from enrich_national_opportunity import write_national_view
# ONE definition of what an advisor is called, shared with build_field_tiles.py.
from display_name import display_name, filed_name
from firm_names import dedupe

ROOT = pathlib.Path(__file__).parents[1]
INTERIM = ROOT / "data" / "interim"
WEB = ROOT / "webapp" / "data"
RAW = ROOT / "data" / "raw"

MOTION_CODE = {"sma_led": 0, "eicix_led": 1, "both_products": 2,
               "low_fit": 3, "unclassified": 4}
FEED_DATE = re.compile(r"(\d{2})_(\d{2})_(\d{4})")


def _source_date(files: list[pathlib.Path]) -> str | None:
    dates = []
    for path in files:
        match = FEED_DATE.search(path.name)
        if match:
            month, day, year = map(int, match.groups())
            dates.append(datetime(year, month, day).date())
    return max(dates).isoformat() if dates else None


def _employment_home() -> dict[str, set]:
    """advisor CRD -> the (state, city) pairs their CURRENT employment records
    report. Rows with no end date are the ones still in force. Used only to
    rank an advisor's filed branches, never to invent a location the branch
    data does not have."""
    history = pd.read_parquet(
        INTERIM / "advisor_employment_history.parquet",
        columns=["advisor_crd", "city", "state", "to_date"],
    )
    current = history[history["to_date"].isna()]
    out: dict[str, set] = {}
    for row in current.itertuples(index=False):
        if pd.isna(row.state) or pd.isna(row.city):
            continue
        key = (str(row.state).strip().upper(), str(row.city).strip().title())
        out.setdefault(str(row.advisor_crd), set()).add(key)
    return out


def _advisor_name(row, used: str = "") -> str:
    """See src/display_name.py. The rule lives there so the field view's
    builder can apply the SAME one -- it did not, and 47,371 advisors had a
    different name on the phone than on the desk."""
    return display_name(
        row.first_name if pd.notna(row.first_name) else "",
        row.last_name if pd.notna(row.last_name) else "",
        used,
    ) or "Name unavailable"


def _filed_name(row) -> str:
    return filed_name(
        row.first_name if pd.notna(row.first_name) else "",
        row.last_name if pd.notna(row.last_name) else "",
        row.middle_name if hasattr(row, "middle_name") and pd.notna(row.middle_name) else "",
    )


def main() -> None:
    WEB.mkdir(parents=True, exist_ok=True)
    firm_facts = pd.read_parquet(
        ROOT / "data" / "output" / "firms.parquet",
        columns=[
            "crd", "motion", "opportunity_score", "raum_total", "g_select_advisers",
            "raum_equity_exchange_implied", "raum_fund_shares_ric_implied",
        ],
    ).rename(columns={"crd": "firm_crd"})
    firm_facts["firm_crd"] = firm_facts["firm_crd"].astype(str)
    feats = []
    per_state = {}
    physical_office_id = 0
    total_rows = placed_rows = pin_rows = 0
    precision = Counter()
    all_advisors = set()
    firm_advisors: dict[str, set[str]] = defaultdict(set)

    # CRD -> accumulated search facts. Counters select the advisor's most common
    # mapped firm/state/city while retaining every state for disclosure.
    advisor_search: dict[str, dict] = {}

    columns = [
        "advisor_crd", "lat", "lon", "branch_street1", "branch_city", "branch_postal",
        "firm_display", "firm_crd", "motion", "opportunity_score",
        "geocode_precision", "first_name", "last_name",
    ]

    # The name a person goes by lives on the advisor table, not the branch
    # parquet, so bring it alongside. Keeping the filed name too lets the search
    # index match both "Tate Lambeth" and "Edison Lambeth".
    used_names = pd.read_parquet(
        ROOT / "data" / "output" / "advisors.parquet",
        columns=["advisor_crd", "used_first_name"],
    )
    used_names["advisor_crd"] = used_names["advisor_crd"].astype(str)
    used_names = dict(zip(used_names["advisor_crd"],
                          used_names["used_first_name"].fillna("")))

    for path in sorted(INTERIM.glob("branch_geocoded_*.parquet")):
        state = path.stem.replace("branch_geocoded_", "")
        source = pd.read_parquet(path, columns=columns)
        source["city_level"] = False
        # Same union as the state layers. If the national view read only
        # street-addressed branches it would place people the state layers
        # place elsewhere, and the two would disagree on where someone works.
        city_path = INTERIM / "branch_city_level.parquet"
        if city_path.exists():
            city = pd.read_parquet(city_path)
            city = city[city["branch_state"].astype(str).str.upper().str.strip() == state]
            if len(city):
                for col in columns:
                    if col not in city.columns:
                        city[col] = None
                source = pd.concat([source, city[list(columns) + ["city_addr_key", "city_level"]]],
                                   ignore_index=True, sort=False)
        total_rows += len(source)
        source["firm_crd"] = source["firm_crd"].astype(str)
        source["advisor_crd"] = source["advisor_crd"].astype(str)
        source = source.drop(columns=["motion", "opportunity_score"]).merge(
            firm_facts, on="firm_crd", how="left"
        )

        data = source[source["lat"].notna() & source["lon"].notna()].copy()
        # Same one-pin-per-advisor-firm rule as the state layers. If national
        # kept every branch row the two levels would report different headcounts
        # for the same firm, and the AUM allocation -- advisors in view divided
        # by the firm's mapped advisors -- would be built on mismatched halves.
        # Geocoding coverage must be measured BEFORE de-duplication. Counting
        # after it reported 75.3% "placed" and 132,707 "unplaced", which reads
        # as a geocoding failure when it is really the duplicate registrations
        # we deliberately drop. Coverage is 99.8%; the pin count is separate.
        placed_rows += len(data)
        precision.update(data["geocode_precision"].fillna("unknown"))
        data = apply_placement(data)
        if data.empty:
            continue
        pin_rows += len(data)
        all_advisors.update(data["advisor_crd"])
        for firm_crd, advisor_ids in data.groupby("firm_crd")["advisor_crd"]:
            firm_advisors[str(firm_crd)].update(advisor_ids.astype(str))

        data["addr"] = data["branch_street1"].fillna("").astype(str).str.strip()
        data["city"] = data["branch_city"].fillna("").astype(str).str.strip()
        data["zip"] = data["branch_postal"].fillna("").astype(str).str.strip().str[:5]
        # Group on the BUILDING, matching the webapp's bldgKey: rounded
        # coordinate plus house number. Filed address strings are noisy -- one
        # Memphis tower is filed six ways -- so grouping on them drew several
        # markers on a single point where only the top one was ever clickable.
        # House number is part of the key because 169 shared coordinates
        # nationally carry different house numbers and must not merge.
        data["house"] = data["addr"].str.extract(r"^\s*(\d+)")[0].fillna("")
        data["bkey"] = (data["lat"].round(5).astype(str) + ","
                        + data["lon"].round(5).astype(str) + "|" + data["house"])
        data["firm_label"] = data["firm_display"].map(display_firm)

        physical = data[["bkey"]].drop_duplicates()
        per_state[state] = {
            "offices": int(len(physical)),
            "advisors": int(data["advisor_crd"].nunique()),
        }
        firm_counts = data.groupby("bkey")["firm_crd"].nunique()

        for bkey, address_group in data.groupby("bkey", sort=False):
            office_id = physical_office_id
            physical_office_id += 1
            firms_here = int(firm_counts.loc[bkey])
            # A repeated filed address can carry tiny row-level geocoder
            # differences. Use its most common coordinate so every legal firm
            # at the physical office lands on the same point and office ID.
            office_lat, office_lon = (
                address_group.groupby(["lat", "lon"], sort=False).size().idxmax()
            )
            for firm_crd, group in address_group.groupby("firm_crd", sort=False):
                group = group.drop_duplicates("advisor_crd")
                motion = group["motion"].mode()
                motion_name = str(motion.iloc[0]) if len(motion) else "unclassified"
                score = group["opportunity_score"].dropna()
                feats.append({
                    "lat": round(float(office_lat), 4),
                    "lon": round(float(office_lon), 4),
                    "n": int(len(group)),
                    "f": str(group["firm_label"].iloc[0]),
                    "crd": str(firm_crd),
                    "score": round(float(score.iloc[0]), 1) if len(score) else None,
                    "raum": (
                        int(round(float(group["raum_total"].dropna().iloc[0]) / 1e6))
                        if len(group["raum_total"].dropna()) else None
                    ),
                    "selects": int(bool(group["g_select_advisers"].fillna(False).iloc[0])),
                    "equity": (
                        round(float(group["raum_equity_exchange_implied"].dropna().iloc[0]) / 1e6, 1)
                        if len(group["raum_equity_exchange_implied"].dropna()) else None
                    ),
                    "funds": (
                        round(float(group["raum_fund_shares_ric_implied"].dropna().iloc[0]) / 1e6, 1)
                        if len(group["raum_fund_shares_ric_implied"].dropna()) else None
                    ),
                    "nf": firms_here,
                    "m": MOTION_CODE.get(motion_name, 4),
                    "s": state,
                    "office_id": office_id,
                })

        for row in data.itertuples(index=False):
            advisor_id = str(row.advisor_crd)
            record = advisor_search.get(advisor_id)
            if record is None:
                record = advisor_search[advisor_id] = {
                    "name": _advisor_name(row, used_names.get(advisor_id, "")),
                    "filed": _filed_name(row),
                    "firms": Counter(),
                    "states": Counter(),
                    "cities": Counter(),
                }
            record["firms"][display_firm(row.firm_display)] += 1
            record["states"][state] += 1
            city = str(row.branch_city).strip().title() if pd.notna(row.branch_city) else ""
            if city:
                record["cities"][(state, city)] += 1

    # Biggest firm-offices last so they retain z-order in circle mode.
    feats.sort(key=lambda row: row["n"])

    firm_ix: dict[str, int] = {}
    firm_list: list[list] = []
    for row in feats:
        if row["crd"] not in firm_ix:
            firm_ix[row["crd"]] = len(firm_list)
            firm_list.append([
                row["f"], row["crd"], row["score"], row["raum"], row["selects"],
                row["equity"], row["funds"], len(firm_advisors[row["crd"]]),
            ])

    states = sorted(per_state)
    state_ix = {state: index for index, state in enumerate(states)}
    packed = [
        [
            row["lon"], row["lat"], row["n"], firm_ix[row["crd"]], row["m"],
            row["nf"], state_ix[row["s"]], row["office_id"],
        ]
        for row in feats
    ]

    national = {"firms": firm_list, "states": states, "offices": packed}
    national_path = WEB / "offices_national.json"
    national_path.write_text(
        json.dumps(national, separators=(",", ":")),
        encoding="utf-8",
    )
    # This is part of the authoritative export, not an optional enrichment.
    # Keeping the compact first-paint artifact beside its detail writer means a
    # normal rebuild cannot validate stale national_view.json by accident.
    write_national_view(national)
    (WEB / "states_index.json").write_text(
        json.dumps(per_state, separators=(",", ":")), encoding="utf-8"
    )

    search_firms: list[str] = []
    search_firm_ix: dict[str, int] = {}
    search_cities: list[str] = []
    search_city_ix: dict[str, int] = {}

    def intern(value, index, values):
        if value not in index:
            index[value] = len(values)
            values.append(value)
        return index[value]

    # Where an advisor is BASED, when their own branches disagree. Counter
    # .most_common breaks a tie by insertion order, so an advisor filed at one
    # office in each of two states was assigned to whichever state file happened
    # to be read first: Thomas Tolleson (CRD 1452953, one Perigon branch in San
    # Francisco and one in Atlanta) resolved to California, while his employment
    # record puts him in Atlanta from 10/2022. The employment feed knows the
    # answer, so rank by it and fall back to branch counts.
    home = _employment_home()

    advisor_rows = []
    for advisor_id, record in advisor_search.items():
        firm = record["firms"].most_common(1)[0][0] if record["firms"] else ""
        based = home.get(advisor_id, set())
        # every (state, city) the advisor is filed at, best first
        places = sorted(
            record["cities"].items(),
            key=lambda item: (item[0] in based, item[1]),
            reverse=True,
        )
        if places:
            state, city = places[0][0]
        else:
            state = record["states"].most_common(1)[0][0] if record["states"] else ""
            city = ""
        row = [
            advisor_id,
            record["name"],
            intern(firm, search_firm_ix, search_firms),
            state,
            intern(city, search_city_ix, search_cities),
            "|".join(sorted(record["states"])),
            # filed legal name, "" when it matches the displayed one. Kept so a
            # search for "Edison Lambeth" still finds the person the map labels
            # "Tate Lambeth".
            "" if record["filed"] == record["name"] else record["filed"],
        ]
        # An advisor filed in more than one place gets the full list so the
        # webapp can offer each location instead of silently choosing one.
        # Nearly 20% of advisors are filed in more than one state, so this is
        # not an edge case -- but the other 80% should not pay for a field they
        # do not need, so it is emitted only when there is a second place.
        if len(places) > 1:
            row.append([
                [place[0], intern(place[1], search_city_ix, search_cities)]
                for place, _ in places
            ])
        advisor_rows.append(row)

    advisor_rows.sort(key=lambda row: (row[1].split()[-1].lower(), row[1].lower(), row[0]))
    (WEB / "advisor_index.json").write_text(
        json.dumps(
            {"firms": search_firms, "cities": search_cities, "advisors": advisor_rows},
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    # Firm search aliases. Kept in their own lazily-fetched file rather than in
    # offices_national.json, which every visit downloads: only a search needs
    # them. Emitted only for firms that actually appear on the map.
    alias_path = ROOT / "data" / "output" / "firm_other_names.parquet"
    firm_aliases: dict[str, list] = {}
    if alias_path.exists():
        raw_alias: dict[str, list] = {}
        for row in pd.read_parquet(alias_path).itertuples(index=False):
            raw_alias.setdefault(str(row.firm_crd), []).append(str(row.other_name))
        for entry in firm_list:
            names = dedupe(entry[0], raw_alias.get(str(entry[1]), []))
            if names:
                firm_aliases[str(entry[1])] = names
    (WEB / "firm_aliases.json").write_text(
        json.dumps(firm_aliases, separators=(",", ":")), encoding="utf-8")
    print(f"{len(firm_aliases):,} mapped firms carry search aliases -> firm_aliases.json")

    raw_files = sorted(path for path in RAW.iterdir() if path.is_file())
    metadata = {
        "source": "SEC Investment Adviser Public Disclosure bulk feeds",
        "source_date": _source_date(raw_files),
        "source_files": [path.name for path in raw_files],
        "refresh_cadence": "On each SEC bulk-feed pipeline run",
        "generated_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "branch_rows": total_rows,
        "placed_rows": placed_rows,
        "pin_rows": pin_rows,
        "unplaced_rows": total_rows - placed_rows,
        "coverage_pct": round(100 * placed_rows / max(total_rows, 1), 3),
        "precision": dict(precision),
        "distinct_advisors": len(all_advisors),
        "firms": len(firm_list),
        "physical_offices": physical_office_id,
        "firm_office_records": len(feats),
        "jurisdictions": len(per_state),
        "advisor_search_records": len(advisor_rows),
    }
    (WEB / "metadata.json").write_text(
        json.dumps(metadata, separators=(",", ":")), encoding="utf-8"
    )

    mb = national_path.stat().st_size / 1024 / 1024
    search_mb = (WEB / "advisor_index.json").stat().st_size / 1024 / 1024
    print(
        f"{physical_office_id:,} physical offices | {len(feats):,} firm-offices | "
        f"{len(firm_list):,} legal firms -> {national_path.name} ({mb:.1f} MB)"
    )
    print(
        f"{placed_rows:,}/{total_rows:,} branch rows placed "
        f"({metadata['coverage_pct']:.3f}%) | {len(all_advisors):,} distinct advisors"
    )
    print(
        f"{len(advisor_rows):,} national advisor search records -> "
        f"advisor_index.json ({search_mb:.1f} MB)"
    )


if __name__ == "__main__":
    main()
