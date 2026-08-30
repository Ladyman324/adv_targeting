"""Build the effective branch geography used by every map export.

The SEC filing remains immutable in ``data/output/advisor_branches.parquet``.
Corrections are applied in memory before state partitioning, with the filed
state/postal retained beside the effective values.  This matters because a
coordinate-only correction still leaves search, lists, territories, and Field
App tiles assigned to the state shard named by the SEC row.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import pathlib
import re

import pandas as pd

from firm_rosters import latest
from normalize_addr import normalize
from geography_integrity import returned_state, USPS_REGIONS


ROOT = pathlib.Path(__file__).resolve().parents[1]
BRANCHES = ROOT / "data" / "output" / "advisor_branches.parquet"
REFERENCE = ROOT / "data" / "reference" / "branch_geography_overrides.csv"
CETERA_CRD = "105644"


class GeographyCorrectionError(RuntimeError):
    """A reviewed correction no longer matches its source evidence."""


def _clean(value: object) -> str:
    if value is None or (not isinstance(value, (str, bytes)) and pd.isna(value)):
        return ""
    return str(value).strip().upper()


def _city_key(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", _clean(value))


def _street_key(value: object) -> str:
    value = "" if value is None or pd.isna(value) else str(value)
    return normalize(value) or re.sub(r"[^A-Z0-9]", "", value.upper())


def _prepare(branches: pd.DataFrame) -> pd.DataFrame:
    out = branches.reset_index(drop=True).copy()
    out["advisor_crd"] = out["advisor_crd"].astype(str)
    out["firm_crd"] = out["firm_crd"].astype(str)
    if "branch_address_source" not in out:
        out["branch_address_source"] = "sec"
    if "filed_branch_state" not in out:
        out["filed_branch_state"] = pd.NA
    if "filed_branch_postal" not in out:
        out["filed_branch_postal"] = pd.NA
    return out


def _replace(out: pd.DataFrame, idx: int, values: dict, source: str) -> None:
    out.at[idx, "filed_branch_state"] = out.at[idx, "branch_state"]
    out.at[idx, "filed_branch_postal"] = out.at[idx, "branch_postal"]
    for field, value in values.items():
        out.at[idx, field] = value
    out.at[idx, "branch_address_source"] = source


def apply_reference_overrides(
    branches: pd.DataFrame, corrections: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, list[dict]]:
    """Apply evidence-bound corrections keyed to the exact old SEC row."""
    out = _prepare(branches)
    if corrections is None:
        if not REFERENCE.exists():
            raise GeographyCorrectionError(
                f"required reviewed geography reference is missing: {REFERENCE}")
        corrections = pd.read_csv(REFERENCE, dtype=str).fillna("")

    report = []
    for correction in corrections.fillna("").to_dict("records"):
        source = _clean(correction.get("source"))
        verified_on = str(correction.get("verified_on") or "").strip()
        old_state = _clean(correction.get("old_state"))
        new_state = _clean(correction.get("new_state"))
        old_postal = str(correction.get("old_postal") or "").strip()
        new_postal = str(correction.get("new_postal") or "").strip()
        if not source:
            raise GeographyCorrectionError("branch correction has no evidence source")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", verified_on):
            raise GeographyCorrectionError(
                f"branch correction for CRD {correction.get('advisor_crd')} "
                "has no valid verification date")
        if old_state not in USPS_REGIONS or new_state not in USPS_REGIONS:
            raise GeographyCorrectionError(
                f"branch correction for CRD {correction.get('advisor_crd')} "
                "has an invalid state")
        if (not re.fullmatch(r"\d{5}(?:-\d{4})?", old_postal)
                or not re.fullmatch(r"\d{5}(?:-\d{4})?", new_postal)):
            raise GeographyCorrectionError(
                f"branch correction for CRD {correction.get('advisor_crd')} "
                "has an invalid postal code")
        pair = (out["advisor_crd"].eq(str(correction["advisor_crd"]))
                & out["firm_crd"].eq(str(correction["firm_crd"])))
        old = (pair
               & out["branch_street1"].map(_clean).eq(_clean(correction["old_street1"]))
               & out["branch_city"].map(_clean).eq(_clean(correction["old_city"]))
               & out["branch_state"].map(_clean).eq(_clean(correction["old_state"]))
               & out["branch_postal"].map(_clean).eq(_clean(correction["old_postal"])))
        if int(old.sum()) != 1:
            raise GeographyCorrectionError(
                f"branch correction for CRD {correction['advisor_crd']} matched "
                f"{int(old.sum())} source rows; review changed evidence"
            )
        idx = int(out.index[old][0])
        values = {
            "branch_street1": correction["new_street1"],
            "branch_street2": correction["new_street2"] or None,
            "branch_city": correction["new_city"],
            "branch_state": correction["new_state"],
            "branch_postal": correction["new_postal"],
            "branch_country": correction["new_country"] or "United States",
        }
        _replace(out, idx, values, correction["source"])
        report.append({"advisor_crd": correction["advisor_crd"],
                       "from": correction["old_state"],
                       "to": correction["new_state"],
                       "source": correction["source"]})
    return out, report


def apply_cetera_roster_corrections(
    branches: pd.DataFrame, roster: pd.DataFrame, source_name: str,
) -> tuple[pd.DataFrame, list[dict]]:
    """Correct only exact-CRD, exact-firm, same-street/city state conflicts.

    Name similarity grants no authority.  A roster row can alter geography
    only when it identifies the same CRD at Cetera and agrees with the SEC row
    on the normalized street and city.  Ambiguous roster locations fail closed.
    """
    out = _prepare(branches)
    required = {"advisor_crd", "address", "address2", "city", "state", "postal"}
    missing = required - set(roster.columns)
    if missing:
        raise GeographyCorrectionError(
            f"{source_name} lacks Cetera geography fields {sorted(missing)}")

    sec = out[out["firm_crd"].eq(CETERA_CRD)].copy()
    sec["_row"] = sec.index
    sec["_street"] = sec["branch_street1"].map(_street_key)
    sec["_city"] = sec["branch_city"].map(_city_key)

    firm = roster.copy().fillna("")
    firm["advisor_crd"] = firm["advisor_crd"].astype(str)
    firm["_street"] = firm["address"].map(_street_key)
    firm["_city"] = firm["city"].map(_city_key)
    joined = sec.merge(firm, on="advisor_crd", suffixes=("_sec", "_roster"))
    candidates = joined[
        joined["_street_sec"].ne("")
        & joined["_street_sec"].eq(joined["_street_roster"])
        & joined["_city_sec"].eq(joined["_city_roster"])
        & joined["branch_state"].map(_clean).ne(joined["state"].map(_clean))
    ].copy()

    report = []
    fields = ["address", "address2", "city", "state", "postal"]
    for row_id, group in candidates.groupby("_row"):
        choices = group[fields].drop_duplicates()
        if len(choices) != 1:
            crd = str(group.iloc[0]["advisor_crd"])
            raise GeographyCorrectionError(
                f"{source_name} gives CRD {crd} {len(choices)} conflicting locations")
        choice = choices.iloc[0]
        if not re.fullmatch(r"[A-Z]{2}", _clean(choice["state"])):
            raise GeographyCorrectionError(
                f"{source_name} gives CRD {group.iloc[0]['advisor_crd']} an invalid state")
        idx = int(row_id)
        old_state = _clean(out.at[idx, "branch_state"])
        values = {
            "branch_street1": choice["address"],
            "branch_street2": choice["address2"] or None,
            "branch_city": choice["city"],
            "branch_state": _clean(choice["state"]),
            "branch_postal": str(choice["postal"]).strip(),
            "branch_country": "United States",
        }
        source = f"firm_roster_exact_crd:{source_name}"
        _replace(out, idx, values, source)
        report.append({"advisor_crd": str(group.iloc[0]["advisor_crd"]),
                       "from": old_state, "to": values["branch_state"],
                       "source": source})
    return out, report


def build_effective_branches(
    branches: pd.DataFrame, roster: pd.DataFrame | None = None,
    corrections: pd.DataFrame | None = None, roster_source: str = "fixture",
) -> tuple[pd.DataFrame, list[dict]]:
    out, report = apply_reference_overrides(branches, corrections)
    if roster is not None:
        out, roster_report = apply_cetera_roster_corrections(
            out, roster, roster_source)
        report.extend(roster_report)
    return out, report


def relocate_geocoded_frames(
    frames: dict[str, pd.DataFrame], effective: pd.DataFrame,
) -> tuple[dict[str, pd.DataFrame], list[dict]]:
    """Move reviewed effective rows to their correct existing state shard.

    This is the no-network repair path for already geocoded data.  A row moves
    only when its existing geocoder display explicitly names the reviewed new
    state.  It therefore cannot turn a source correction into an invented
    coordinate or silently move an ambiguous point.
    """
    out = {state: frame.copy() for state, frame in frames.items()}
    corrected = effective[effective["branch_address_source"].ne("sec")].copy()
    report = []
    for row in corrected.itertuples():
        old_state = _clean(row.filed_branch_state)
        new_state = _clean(row.branch_state)
        if not old_state or not new_state or old_state == new_state:
            raise GeographyCorrectionError(
                f"CRD {row.advisor_crd} lacks a cross-state filed/effective pair")
        if old_state not in out or new_state not in out:
            raise GeographyCorrectionError(
                f"CRD {row.advisor_crd} requires missing shard {old_state} or {new_state}")

        source = out[old_state]
        hit = (source["advisor_crd"].astype(str).eq(str(row.advisor_crd))
               & source["firm_crd"].astype(str).eq(str(row.firm_crd))
               & source["branch_state"].map(_clean).eq(old_state)
               & source["branch_postal"].map(_clean).eq(_clean(row.filed_branch_postal)))
        if int(hit.sum()) == 0:
            target = out[new_state]
            already = (target["advisor_crd"].astype(str).eq(str(row.advisor_crd))
                       & target["firm_crd"].astype(str).eq(str(row.firm_crd))
                       & target["branch_state"].map(_clean).eq(new_state)
                       & target["branch_postal"].map(_clean).eq(_clean(row.branch_postal)))
            if int(already.sum()) == 1 and returned_state(
                    target.loc[already].iloc[0].get("matched", "")) == new_state:
                report.append({"advisor_crd": str(row.advisor_crd),
                               "from": old_state, "to": new_state,
                               "source": row.branch_address_source,
                               "status": "already_applied"})
                continue
        if int(hit.sum()) != 1:
            raise GeographyCorrectionError(
                f"CRD {row.advisor_crd} matched {int(hit.sum())} rows in {old_state} shard")
        moved = source.loc[hit].copy()
        actual = returned_state(moved.iloc[0].get("matched", ""))
        if actual != new_state:
            raise GeographyCorrectionError(
                f"CRD {row.advisor_crd} cannot move {old_state}->{new_state}; "
                f"existing geocoder evidence says {actual or 'nothing'}")

        for field in ("branch_street1", "branch_street2", "branch_city",
                      "branch_state", "branch_postal", "branch_country",
                      "branch_address_source", "filed_branch_state",
                      "filed_branch_postal"):
            moved.loc[:, field] = getattr(row, field)
        moved.loc[:, "addr_key"] = (
            _clean(row.branch_street1) + "|" + _clean(row.branch_city) + "|"
            + new_state + "|" + str(row.branch_postal).strip())
        out[old_state] = source.loc[~hit].copy()
        out[new_state] = pd.concat([out[new_state], moved], ignore_index=True,
                                   sort=False)
        report.append({"advisor_crd": str(row.advisor_crd),
                       "from": old_state, "to": new_state,
                       "source": row.branch_address_source,
                       "status": "moved"})
    return out, report


def repair_existing_geocoded(apply: bool = False) -> list[dict]:
    """Validate and optionally write the local, no-network shard repairs."""
    states = sorted({
        path.stem.replace("branch_geocoded_", "")
        for path in (ROOT / "data" / "interim").glob("branch_geocoded_*.parquet")
    })
    frames = {
        state: pd.read_parquet(
            ROOT / "data" / "interim" / f"branch_geocoded_{state}.parquet")
        for state in states
    }
    repaired, report = relocate_geocoded_frames(frames, effective_branches())
    for row in report:
        print(f"  local shard repair ({row['status']}): CRD {row['advisor_crd']} "
              f"{row['from']} -> {row['to']} ({row['source']})")
    if apply:
        touched = {row["from"] for row in report} | {row["to"] for row in report}
        for state in sorted(touched):
            repaired[state].to_parquet(
                ROOT / "data" / "interim" / f"branch_geocoded_{state}.parquet",
                index=False)
        print(f"APPLIED {len(report)} correction(s) across {len(touched)} state shards")
    else:
        print(f"DRY RUN: {len(report)} correction(s); re-run with --apply-geocoded")
    return report


@lru_cache(maxsize=1)
def effective_branches() -> pd.DataFrame:
    """Load SEC branches once and overlay current reviewed geography evidence."""
    roster_path = latest("cetera")
    roster = pd.read_csv(roster_path, dtype=str).fillna("") if roster_path else None
    out, report = build_effective_branches(
        pd.read_parquet(BRANCHES), roster=roster,
        roster_source=roster_path.name if roster_path else "missing")
    for row in report:
        print(f"  geography correction: CRD {row['advisor_crd']} "
              f"{row['from']} -> {row['to']} ({row['source']})")
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply-geocoded", action="store_true",
                        help="write validated local state-shard repairs")
    args = parser.parse_args()
    data = effective_branches()
    corrected = data[data["branch_address_source"].ne("sec")]
    print(f"{len(corrected):,} effective branch correction(s); SEC source unchanged")
    repair_existing_geocoded(args.apply_geocoded)
