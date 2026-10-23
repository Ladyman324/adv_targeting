"""Offline rebuild of fresh SEC state carriers using exact-address cached points."""
from __future__ import annotations

import argparse
import pathlib
import re
import pandas as pd


ADDRESS = ["branch_street1", "branch_street2", "branch_city", "branch_state", "branch_postal"]
COORDINATES = ["lat", "lon", "geocode_precision", "matched", "geocode_source"]


def address_keys(frame):
    # Include unit/line2 and the full postal code: uncertain equivalence must
    # not silently retain coordinates for a different physical address.
    return frame.reindex(columns=ADDRESS).fillna("").astype(str).apply(
        lambda col: col.str.upper().str.strip()).agg("|".join, axis=1)


def cached_geography(stage_root):
    root = pathlib.Path(stage_root)
    interim, output = root / "data/interim", root / "data/output"
    branches = pd.read_parquet(output / "advisor_branches.parquet")
    required = {"advisor_crd", "firm_crd", "branch_state", "branch_street1"}
    if not required.issubset(branches.columns):
        raise ValueError("Fresh SEC branches lack required identity/address columns")
    # Reuse exact, evidence-bound reference corrections, but never source-backed
    # roster/ACT locations here; those retain their separate later placement step.
    import branch_geography
    reference = root / "data/reference/branch_geography_overrides.csv"
    if reference.exists():
        corrections = pd.read_csv(reference, dtype=str).fillna("")
        branches, _ = branch_geography.apply_reference_overrides(branches, corrections)
    from firm_rosters import NAME_RE
    roster_paths = [p for p in (root / "data/raw/firm_rosters").glob("cetera_*.csv")
                    if NAME_RE.match(p.name)]
    if roster_paths:
        roster = max(roster_paths, key=lambda p: p.name)
        branches, _ = branch_geography.apply_cetera_roster_corrections(
            branches, pd.read_csv(roster, dtype=str).fillna(""), roster.name)
    paths = sorted(interim.glob("branch_geocoded_*.parquet"))
    caches = []
    for path in paths:
        old = pd.read_parquet(path)
        if not {"lat", "lon"}.issubset(old.columns):
            continue
        old = old[old.lat.notna() & old.lon.notna()].copy()
        from geography_integrity import validate_geocoded_frame
        validate_geocoded_frame(old, path.stem[len("branch_geocoded_"):], path.name)
        old = old[old.lat.between(-90, 90) & old.lon.between(-180, 180)]
        old["_address"] = address_keys(old)
        caches.append(old.reindex(columns=["_address"] + COORDINATES))
    cache = (pd.concat(caches, ignore_index=True).drop_duplicates("_address")
             if caches else pd.DataFrame(columns=["_address"] + COORDINATES))
    branches = branches.drop(columns=COORDINATES + ["addr_key"], errors="ignore").copy()
    branches["_address"] = address_keys(branches)
    branches = branches.merge(cache, on="_address", how="left").drop(columns="_address")
    branches["addr_key"] = address_keys(branches)
    for name in ("advisor_crd", "firm_crd"):
        branches[name] = branches[name].astype(str)
    advisors = pd.read_parquet(output / "advisors.parquet")[["advisor_crd", "first_name", "last_name"]].copy()
    advisors["advisor_crd"] = advisors.advisor_crd.astype(str)
    firms = pd.read_parquet(output / "firms.parquet")[["crd", "firm_display", "opportunity_score", "motion"]].copy()
    firms["crd"] = firms.crd.astype(str)
    branches = branches.merge(advisors, on="advisor_crd", how="left").merge(
        firms, left_on="firm_crd", right_on="crd", how="left")
    states = sorted(str(s) for s in branches.branch_state.dropna().unique()
                    if re.fullmatch("[A-Z]{2}", str(s)))
    interim.mkdir(parents=True, exist_ok=True)
    summary = {}
    for state in states:
        frame = branches[branches.branch_state.eq(state)]
        frame.to_parquet(interim / f"branch_geocoded_{state}.parquet", index=False)
        summary[state] = {"rows": len(frame), "placed": int(frame.lat.notna().sum()),
                          "unresolved": int(frame.lat.isna().sum())}
    for path in paths:
        if path.stem[len("branch_geocoded_"):] not in states:
            path.unlink()  # staged, explicitly obsolete state shard only
    # City-only SEC branches are a separate placement input. Rebuild membership
    # too; otherwise deleted people can survive in the old city fallback.
    city_source = root / "webapp/data/geo_index.json"
    city_target = interim / "branch_city_level.parquet"
    if city_source.exists():
        import geocode_city_only
        previous = geocode_city_only.ROOT, geocode_city_only.INTERIM, geocode_city_only.WEB
        try:
            geocode_city_only.ROOT, geocode_city_only.INTERIM, geocode_city_only.WEB = root, interim, root / "webapp/data"
            geocode_city_only.main()
        finally:
            geocode_city_only.ROOT, geocode_city_only.INTERIM, geocode_city_only.WEB = previous
    elif city_target.exists():
        raise ValueError("Cannot validate prior city-only membership: staged geo_index.json is missing")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=pathlib.Path, required=True)
    print(cached_geography(parser.parse_args().root))
