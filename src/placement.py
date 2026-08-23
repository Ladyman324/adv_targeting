"""Choose ONE mapped location per advisor-firm relationship.

The map drew one pin per advisor-branch pairing, which is not one pin per
person: 535,001 pins covered 397,551 advisors. The duplicates are not random --
they concentrate at registered addresses. 3,737 advisors are filed at Merrill's
One Bryant Park and only 1,000 have any New York employment record; 466 Steward
Partners advisors are filed at a single Stamford suite, "FLOOR 10, SUITE 1020",
and 4% have a Connecticut record. Those addresses became the largest markers on
the map while being the least real.

The filing corroborates itself: an advisor's CURRENT employment history names a
city, and 70.3% of advisors have exactly one filed branch in a city their
employment history names. That branch is the one they work at; the others are
registrations.

Deliberately NOT done here: moving advisors whose employment history names a
place they have no branch in. That is 24% of advisors, and they do not scatter
-- 74,461 of them would land in a city receiving 100 or more, with St. Louis
alone taking 20,775 across three spellings of its name. Replacing a wrong
building with a wrong point is not an improvement, so those advisors stay at
their filed address and are marked uncertain instead.

Writes data/interim/advisor_placement.parquet:
    advisor_crd, firm_crd, addr_key, uncertain, home_label
one row per advisor-firm pair, naming the branch that keeps its pin.
"""
from __future__ import annotations

import glob
import json
import pathlib

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
INTERIM = ROOT / "data" / "interim"


def addr_key(street, city, postal) -> pd.Series:
    """Join key shared with export_geojson. Normalised so casing and stray
    whitespace in the filed strings cannot break the match."""
    return (street.fillna("").astype(str).str.strip().str.upper() + "|"
            + city.fillna("").astype(str).str.strip().str.upper() + "|"
            + postal.fillna("").astype(str).str.strip().str[:5])


def load_branches() -> pd.DataFrame:
    frames = [pd.read_parquet(path, columns=[
        "advisor_crd", "firm_crd", "branch_street1", "branch_city",
        "branch_state", "branch_postal", "lat"])
        for path in glob.glob(str(INTERIM / "branch_geocoded_*.parquet"))]
    br = pd.concat(frames, ignore_index=True)
    br = br[br["lat"].notna()].copy()
    br["advisor_crd"] = br["advisor_crd"].astype(str)
    br["firm_crd"] = br["firm_crd"].astype(str)
    br["addr_key"] = addr_key(br["branch_street1"], br["branch_city"],
                              br["branch_postal"])
    br["city_level"] = False

    # City-only branches, which the geocoder could not place and which were
    # therefore missing entirely rather than ranked last. Without them a firm's
    # own statement that someone works in Villanova loses by default to its
    # headquarters, which is how CRD 4223682 ended up in Atlanta.
    city_path = INTERIM / "branch_city_level.parquet"
    if city_path.exists():
        city = pd.read_parquet(city_path, columns=[
            "advisor_crd", "firm_crd", "branch_street1", "branch_city",
            "branch_state", "branch_postal", "lat", "city_addr_key", "city_level"])
        city = city.rename(columns={"city_addr_key": "addr_key"})
        city["advisor_crd"] = city["advisor_crd"].astype(str)
        city["firm_crd"] = city["firm_crd"].astype(str)
        br = pd.concat([br, city], ignore_index=True)
    br["city_key"] = (br["branch_state"].fillna("").str.upper().str.strip() + "|"
                      + br["branch_city"].fillna("").str.upper().str.strip())
    br["has_street"] = br["branch_street1"].fillna("").astype(str).str.strip() != ""
    return br


def load_home() -> tuple:
    """advisor -> the (STATE|CITY) keys their current employment names, plus a
    readable label for the panel."""
    hist = pd.read_parquet(INTERIM / "advisor_employment_history.parquet",
                           columns=["advisor_crd", "city", "state", "to_date"])
    hist = hist[hist["to_date"].isna()].dropna(subset=["state", "city"])
    hist["advisor_crd"] = hist["advisor_crd"].astype(str)
    hist["key"] = (hist["state"].str.upper().str.strip() + "|"
                   + hist["city"].str.upper().str.strip())
    hist["label"] = (hist["city"].str.strip().str.title() + ", "
                     + hist["state"].str.upper().str.strip())
    keys = hist.groupby("advisor_crd")["key"].apply(set).to_dict()
    labels = hist.groupby("advisor_crd")["label"].apply(
        lambda s: " · ".join(sorted(set(s))[:3])).to_dict()
    return keys, labels


def main() -> None:
    br = load_branches()
    home_keys, home_labels = load_home()

    # How many DIFFERENT advisors are filed at each address. A registered
    # address carries hundreds; a real office of one carries one. Used only to
    # break ties, never to decide on its own.
    crowd = br.groupby("addr_key")["advisor_crd"].nunique().to_dict()

    # Vectorised: one pass, no per-group Python. Iterating 405k groups and
    # sorting inside each was minutes of work for a decision that is three
    # column comparisons.
    home_df = pd.DataFrame(
        [(crd, key) for crd, keys in home_keys.items() for key in keys],
        columns=["advisor_crd", "city_key"])
    home_df["hit"] = True
    br = br.merge(home_df, on=["advisor_crd", "city_key"], how="left")
    br["hit"] = br["hit"].fillna(False)
    br["has_history"] = br["advisor_crd"].isin(home_keys)
    br["crowd"] = br["addr_key"].map(crowd).fillna(0)

    # State-level corroboration, as a coarser fallback to the city match.
    # Villanova and Berwyn are five miles apart on the same commuter line, so a
    # city-exact test calls them unrelated. At state level they agree, which is
    # the resolution a territory is drawn at anyway.
    home_state_df = pd.DataFrame(
        [(crd, key.split("|")[0]) for crd, keys in home_keys.items() for key in keys],
        columns=["advisor_crd", "branch_state"]).drop_duplicates()
    home_state_df["hit_state"] = True
    br["branch_state"] = br["branch_state"].fillna("").astype(str).str.upper().str.strip()
    br = br.merge(home_state_df, on=["advisor_crd", "branch_state"], how="left")
    br["hit_state"] = br["hit_state"].fillna(False)

    # Is this branch the firm's own headquarters? A firm's employment record
    # names the firm's address, so for anyone employed there it corroborates
    # itself: EIC's record says Atlanta for every EIC employee regardless of
    # where they sit. Headquarters is the null hypothesis, not evidence.
    profiles = json.loads(
        (ROOT / "webapp" / "data" / "firm_profiles.json").read_text(encoding="utf-8"))
    # City AND state, not state alone: matching on state made every Merrill
    # office in New York "headquarters" and pushed a quarter of all placements
    # into the weak bucket.
    hq = {crd: (str(p.get("city") or "").upper().strip(),
                str(p.get("state") or "").upper().strip())
          for crd, p in profiles["profiles"].items()}
    branch_city_u = br["branch_city"].fillna("").astype(str).str.upper().str.strip()
    br["is_hq"] = [hq.get(f, ("", "")) == (c, s) and s != ""
                   for f, c, s in zip(br["firm_crd"], branch_city_u, br["branch_state"])]

    # Evidence score. City agreement beats state agreement; headquarters is
    # discounted because it corroborates the firm rather than the person.
    #   3  city matches, not headquarters      2  state matches, not headquarters
    #   1  city matches, is headquarters       0  state-only at headquarters, or nothing
    br["score"] = (br["hit"].map({True: 3, False: 0})
                   .where(br["hit"], br["hit_state"].map({True: 2, False: 0})))
    br.loc[br["is_hq"], "score"] = (br.loc[br["is_hq"], "score"] - 2).clip(lower=0)

    pair = ["advisor_crd", "firm_crd"]
    br["pair_hit"] = br.groupby(pair)["hit"].transform("max").astype(bool)
    br["best_score"] = br.groupby(pair)["score"].transform("max")

    # Evidence first, precision second. The old order put has_street first,
    # which let address precision decide where somebody works -- a geocoding
    # property standing in for a fact about a person.
    eligible = br.sort_values(
        ["advisor_crd", "firm_crd", "score", "has_street", "crowd"],
        ascending=[True, True, False, False, True])
    picked = eligible.drop_duplicates(pair, keep="first").copy()

    # One boolean cannot say "we know the town but not the street", which is a
    # different claim from "we do not know". Each type implies a different
    # action for a rep: visit, call, treat the firm as the entity, or verify.
    # Three types, not four. A "firm_default" tier keyed on is_hq was wrong:
    # it labelled everyone who genuinely works at a single-office firm's only
    # address as weakly placed -- 10 of EIC's 16 advisors, all of them correct.
    # Being at headquarters is not itself doubt; having no corroboration is,
    # and that is already "uncertain".
    picked["location_type"] = "office"
    picked.loc[picked["city_level"], "location_type"] = "remote"
    picked.loc[picked["score"] == 0, "location_type"] = "uncertain"
    picked["uncertain"] = picked["location_type"] == "uncertain"
    picked["home_label"] = picked["advisor_crd"].map(home_labels).fillna("")
    picked.loc[~picked["uncertain"], "home_label"] = ""

    hits = br.groupby(pair)["hit"].sum()
    stats = {t: int((picked["location_type"] == t).sum())
             for t in ("office", "remote", "uncertain")}
    out = picked[["advisor_crd", "firm_crd", "addr_key", "uncertain",
                  "location_type", "home_label"]]

    out.to_parquet(INTERIM / "advisor_placement.parquet", index=False)
    total = len(out)
    print(f"advisor_placement.parquet  {total:,} advisor-firm placements "
          f"from {len(br):,} mapped branch rows "
          f"({(1 - total / len(br)) * 100:.1f}% fewer pins)")
    for name, n in stats.items():
        print(f"  {name:<12}{n:>8,}  ({n / total * 100:5.1f}%)")

    # promised follow-up: which firms drive the uncertain bucket
    unc = out[out["uncertain"]]
    firms = pd.read_parquet(ROOT / "data" / "output" / "firms.parquet",
                            columns=["crd", "name"])
    firms["crd"] = firms["crd"].astype(str)
    names = dict(zip(firms["crd"], firms["name"]))
    top = unc["firm_crd"].value_counts().head(12)
    print(f"\nuncertain placements by firm (top 12 of {unc['firm_crd'].nunique():,}):")
    for crd, n in top.items():
        print(f"  {n:6,}  {names.get(crd, crd)[:46]}  (CRD {crd})")
    print(f"  top 12 account for {top.sum() / len(unc) * 100:.1f}% of all uncertain placements")


if __name__ == "__main__":
    main()
