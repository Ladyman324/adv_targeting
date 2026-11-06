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


def person_address_keys(frame):
    """Bind a prior point to the same SEC person, firm and full street address."""
    if frame.empty:
        return pd.Series(index=frame.index, dtype=object)
    ids = frame.reindex(columns=["advisor_crd", "firm_crd"]).fillna("").astype(str)
    valid = ids.apply(lambda col: col.str.fullmatch(r"[1-9][0-9]*")).all(axis=1)
    street = frame.reindex(columns=["branch_street1"]).fillna("").iloc[:, 0].astype(str)
    return (ids.advisor_crd + "|" + ids.firm_crd + "|" + address_keys(frame)).where(
        valid & street.str.strip().ne(""))


def building_keys(frame):
    """Conservative building identity, not a fuzzy street or city match.

    Normalize explicit street abbreviations, suite/floor and ZIP+4 only.
    House number, directional, street, city, state and ZIP5 must still agree.
    Building designators are retained: a campus is not necessarily one pin.
    """
    data = frame.reindex(columns=ADDRESS).fillna("").astype(str).apply(
        lambda col: col.str.upper().str.strip().str.replace(r"\s+", " ", regex=True))
    from normalize_addr import normalize
    def street(value):
        if re.search(r"\b(?:BLDG|BUILDING)\b", value):
            return value
        return normalize(value)
    data["branch_street1"] = data.branch_street1.map(street)
    postal = data.branch_postal.str.extract(r"^(\d{5})(?:-?\d{4})?$", expand=False)
    valid = (postal.notna() & data.branch_street1.str.match(r"^\d+\s+\S+")
             & data.branch_city.ne("") & data.branch_state.str.fullmatch(r"[A-Z]{2}"))
    data["branch_postal"] = postal
    keys = data[["branch_street1", "branch_city", "branch_state", "branch_postal"]].fillna("").agg("|".join, axis=1)
    return keys.where(valid)


def unambiguous_points(frame, key):
    """Never let file order choose between conflicting cached coordinates."""
    present = frame[frame[key].notna()].copy()
    locations = present[[key, "lat", "lon"]].copy()
    locations[["lat", "lon"]] = locations[["lat", "lon"]].round(5)
    conflicts = locations.drop_duplicates().groupby(key).size()
    safe = set(conflicts[conflicts.eq(1)].index)
    return present[present[key].isin(safe)].drop_duplicates(key)[[key] + COORDINATES]


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
        old["_person_address"] = person_address_keys(old)
        old["_building"] = building_keys(old)
        caches.append(old.reindex(columns=["_address", "_person_address", "_building"] + COORDINATES))
    prior = (pd.concat(caches, ignore_index=True) if caches else
             pd.DataFrame(columns=["_address", "_person_address", "_building"] + COORDINATES))
    cache = unambiguous_points(prior, "_address")
    branches = branches.drop(columns=COORDINATES + ["addr_key"], errors="ignore").copy()
    branches["_address"] = address_keys(branches)
    branches = branches.merge(cache, on="_address", how="left").drop(columns="_address")
    # Another person's conflicting point must not erase this person's own
    # unambiguous prior point at an unchanged firm and full address.
    personal = unambiguous_points(prior, "_person_address").set_index("_person_address")
    personal_keys = person_address_keys(branches)
    known = personal_keys.isin(personal.index)
    for column in COORDINATES:
        branches.loc[known, column] = personal_keys[known].map(personal[column])
    branches["_building"] = building_keys(branches)
    fallback = unambiguous_points(prior, "_building").set_index("_building")
    missing = branches.lat.isna() & branches._building.isin(fallback.index)
    for column in COORDINATES:
        branches.loc[missing, column] = branches.loc[missing, "_building"].map(fallback[column])
    branches = branches.drop(columns="_building")
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
