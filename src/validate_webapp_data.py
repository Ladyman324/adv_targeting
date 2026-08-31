"""Validate identity and count invariants across generated map artifacts."""
from __future__ import annotations

from collections import defaultdict
import json
import pathlib


ROOT = pathlib.Path(__file__).parents[1]
WEB = ROOT / "webapp" / "data"


def read(name: str):
    return json.loads((WEB / name).read_text(encoding="utf-8"))


NATIONAL_CELL = 0.25


def expected_national_grid(national: dict) -> list[list]:
    """Independently derive the compact grid from authoritative detail."""
    grid: dict[tuple[int, float, float], list[int]] = defaultdict(
        lambda: [0, 0]
    )
    firms = national["firms"]
    for office in national["offices"]:
        lon, lat, count, firm_index = office[:4]
        state_index = office[6]
        cell_lat = round(round(lat / NATIONAL_CELL) * NATIONAL_CELL, 3)
        cell_lon = round(round(lon / NATIONAL_CELL) * NATIONAL_CELL, 3)
        totals = grid[(state_index, cell_lat, cell_lon)]
        totals[0] += count
        if firms[firm_index][4]:
            totals[1] += count
    return [
        [lat, lon, counts[0], counts[1], state_index]
        for (state_index, lat, lon), counts in sorted(grid.items())
    ]


def validate_national_view(view: dict, national: dict) -> None:
    """Assert that first-paint data is a faithful subset of full detail."""
    if set(view) != {"states", "grid"}:
        raise SystemExit(
            "national_view.json must contain only states and grid; re-run "
            "src/enrich_national_opportunity.py")
    if view["states"] != national["states"]:
        raise SystemExit(
            "national_view.json states are not the ordered states from "
            "offices_national.json; re-run src/enrich_national_opportunity.py")
    rows = view["grid"]
    if not rows:
        raise SystemExit("national_view.json carries no grid cells")
    if any(not isinstance(cell, list) or len(cell) != 5 for cell in rows):
        raise SystemExit("national_view.json grid rows must be 5 fields wide")
    if any(not isinstance(cell[4], int)
           or not 0 <= cell[4] < len(view["states"]) for cell in rows):
        raise SystemExit("national_view.json grid has an invalid state index")
    if any(not isinstance(cell[2], int) or cell[2] <= 0
           or not isinstance(cell[3], int) or not 0 <= cell[3] <= cell[2]
           for cell in rows):
        raise SystemExit("national_view.json grid has invalid placement counts")
    keys = [(cell[4], cell[0], cell[1]) for cell in rows]
    if len(keys) != len(set(keys)):
        raise SystemExit("national_view.json has duplicate state/grid cells")

    expected = expected_national_grid(national)
    all_total = sum(cell[2] for cell in rows)
    expected_all = sum(office[2] for office in national["offices"])
    if all_total != expected_all:
        raise SystemExit(
            f"national_view.json all-count totals {all_total:,} placements, "
            f"the office array totals {expected_all:,}")
    selecting_total = sum(cell[3] for cell in rows)
    expected_selecting = sum(
        office[2] for office in national["offices"]
        if national["firms"][office[3]][4]
    )
    if selecting_total != expected_selecting:
        raise SystemExit(
            f"national_view.json selecting-count totals {selecting_total:,} "
            f"placements, detail totals {expected_selecting:,}")
    if rows != expected:
        raise SystemExit(
            "national_view.json grid is not the deterministic per-state "
            "aggregate of offices_national.json")


def main() -> None:
    metadata = read("metadata.json")
    national = read("offices_national.json")
    # The map's OPENING payload is a separate artifact, derived from the file
    # above. A stale or missing one is invisible at runtime -- the app would
    # simply fall back to an empty grid and a heat map with nothing in it --
    # so it is checked here rather than discovered by a rep.
    view = read("national_view.json")
    validate_national_view(view, national)
    state_index = read("states_index.json")
    profiles = read("firm_profiles.json")

    firms = national["firms"]
    offices = national["offices"]
    states = national["states"]
    firm_crds = [str(firm[1]) for firm in firms]
    assert all(len(firm) == 9 for firm in firms), (
        "national firm rows must include opportunity-pool and outside-manager reason fields")
    assert all(firm[3] is None or firm[3] >= 0 for firm in firms), "invalid national firm AUM"
    assert all(firm[4] in (0, 1) for firm in firms), "invalid national outside-manager flag"
    assert all(firm[5] is None or firm[5] >= 0 for firm in firms), "invalid equity opportunity pool"
    assert all(firm[6] is None or firm[6] >= 0 for firm in firms), "invalid fund opportunity pool"
    assert all(isinstance(firm[7], int) and firm[7] > 0 for firm in firms), "invalid mapped-advisor total"
    assert all(firm[8] in ("", "selects", "wrap", "both") for firm in firms), (
        "invalid national outside-manager reason")
    assert len(firm_crds) == len(set(firm_crds)), "duplicate CRD in national firms"
    assert len(firms) == metadata["firms"]
    assert len(offices) == metadata["firm_office_records"]
    assert set(states) == set(state_index)

    office_firms: dict[int, set[str]] = defaultdict(set)
    office_location = {}
    state_offices: dict[str, set[int]] = defaultdict(set)
    for lon, lat, count, firm_index, _motion, firms_here, state_index_value, office_id in offices:
        assert 0 <= firm_index < len(firms)
        assert 0 <= state_index_value < len(states)
        assert count > 0
        state = states[state_index_value]
        location = (lon, lat, state, firms_here)
        assert office_location.setdefault(office_id, location) == location
        office_firms[office_id].add(firm_crds[firm_index])
        state_offices[state].add(office_id)
    assert len(office_firms) == metadata["physical_offices"]
    for office_id, crds in office_firms.items():
        assert len(crds) == office_location[office_id][3], f"firm count mismatch at office {office_id}"
    for state, ids in state_offices.items():
        assert len(ids) == state_index[state]["offices"], f"office total mismatch for {state}"

    state_pin_total = 0
    state_firm_crds = set()
    for path in sorted(WEB.glob("pins_??.json")):
        layer = json.loads(path.read_text(encoding="utf-8"))
        crds = [str(firm[7]) for firm in layer["firms"]]
        assert all(crd.isdigit() for crd in crds), f"invalid CRD in {path.name}"
        assert len(crds) == len(set(crds)), f"duplicate firm CRD in {path.name}"
        assert all(0 <= pin[2] < len(crds) for pin in layer["pins"])
        state_pin_total += len(layer["pins"])
        state_firm_crds.update(crds)
    # The state layers carry one pin per advisor-firm placement, NOT one per
    # geocoded branch row. Those diverged when de-duplication landed: 535,001
    # branch rows geocode, 403,606 survive placement. This assertion still
    # compared against placed_rows and had been failing on every build since --
    # and because nothing invoked this file, nobody found out.
    assert state_pin_total == metadata["pin_rows"], (
        f"state pins {state_pin_total:,} != metadata pin_rows {metadata['pin_rows']:,}")
    assert metadata["pin_rows"] <= metadata["placed_rows"], (
        "placement cannot produce more pins than there are geocoded rows")

    # Pin rows are read positionally by the webapp's rehydrate(); a field
    # inserted mid-array rather than appended silently shifts everything after
    # it, which has happened once already.
    PIN_WIDTH = 18
    for path in sorted(WEB.glob("pins_??.json")):
        layer = json.loads(path.read_text(encoding="utf-8"))
        assert layer.get("schema") == 2, f"{path.name}: unsupported pin schema"
        bad = [p for p in layer["pins"] if len(p) != PIN_WIDTH]
        assert not bad, f"{path.name}: {len(bad):,} pins are not {PIN_WIDTH} fields wide"
        # location type must be one of the three codes, and an uncertain pin
        # must agree with the older boolean it superseded
        assert all(p[16] in (0, 1, 2) for p in layer["pins"]),             f"{path.name}: invalid location-type code"
        assert all((p[14] == 1) == (p[16] == 2) for p in layer["pins"]),             f"{path.name}: uncertain flag disagrees with location type"
        assert all(p[17] is None or isinstance(p[17], int) for p in layer["pins"]),             f"{path.name}: joined-firm day must be an integer or null"
    assert state_firm_crds == set(firm_crds), "state/national legal-firm sets differ"
    assert set(profiles["profiles"]) == set(firm_crds), "firm-profile/national legal-firm sets differ"
    assert len(profiles["client_labels"]) == 14
    assert len(profiles["asset_labels"]) == 12
    valid_products = {"sma_led", "eicix_led", "both_products", "low_fit"}
    for crd, profile in profiles["profiles"].items():
        assert profile["product"] in valid_products, f"invalid product opportunity for {crd}"
        assert len(profile["clients"]) == 14, f"client table length mismatch for {crd}"
        assert len(profile["assets"]) == 12, f"asset table length mismatch for {crd}"

    search = read("advisor_index.json")
    advisor_ids = [str(row[0]) for row in search["advisors"]]
    assert len(advisor_ids) == metadata["advisor_search_records"]
    assert len(advisor_ids) == len(set(advisor_ids)), "duplicate advisor in national search"
    assert all(0 <= row[2] < len(search["firms"]) for row in search["advisors"])
    assert all(0 <= row[4] < len(search["cities"]) for row in search["advisors"])

    # A few named records that exercise the paths most likely to break silently.
    profiles_by_crd = profiles["profiles"]
    for crd, note in [("283930", "Equity Investment Corp"),
                      ("153235", "Red Door Wealth"),
                      ("107342", "Fisher Investments")]:
        assert crd in profiles_by_crd, f"missing firm profile for {note} (CRD {crd})"

    owners = read("owner_roles.json")
    assert owners["roles"], "owner_roles.json carries no roles"
    assert all(0 <= role[1] < len(owners["titles"])
               for rows in owners["roles"].values() for role in rows),         "owner role points at a title index that does not exist"

    aliases = read("firm_aliases.json")
    assert aliases, "firm_aliases.json is empty"

    # Barron's is optional -- it comes from a browser harvest, not the SEC
    # feeds -- but if it shipped it has to be usable.
    barrons_path = WEB / "barrons.json"
    barrons_note = "Barron's: not built"
    if barrons_path.exists():
        barrons = read("barrons.json")
        assert barrons["advisors"], "barrons.json carries no advisors"
        entries = [e for rows in barrons["advisors"].values() for e in rows]
        assert all(len(e) == 5 for e in entries), "barrons entry is not 5 wide"
        assert all(e[0] in barrons["labels"] for e in entries),             "barrons entry names a list with no label"
        # a state rank without a state would render as a bare "#1", which is
        # the ambiguity the whole labelling scheme exists to prevent
        assert all(e[2] for e in entries if e[0] == "top1500"),             "state-ranked barrons entry is missing its state"
        matched = sum(1 for crd in barrons["advisors"] if crd in advisor_ids)
        barrons_note = (f"Barron's: {len(barrons['advisors']):,} ranked advisors, "
                        f"{len(entries):,} rankings, {matched:,} matched to a mapped advisor")

    placements = sum(row[2] for row in offices)
    print(
        f"Validated {len(firms):,} CRD-keyed firms, {len(offices):,} firm-offices, "
        f"{len(office_firms):,} physical offices, and {len(advisor_ids):,} searchable advisors."
    )
    print(
        f"State pins: {state_pin_total:,} (from {metadata['placed_rows']:,} geocoded branch "
        f"rows, {metadata['placed_rows'] - state_pin_total:,} removed as duplicate "
        f"registrations); national firm-office placements: {placements:,}; "
        f"source date: {metadata['source_date']}."
    )
    print(f"Owner roles: {len(owners['roles']):,} advisors; "
          f"firm aliases: {len(aliases):,} firms.")
    # Forbes is optional and, unlike Barron's, every CRD here is either
    # bridged or inferred -- so the gate checks that nothing below the
    # shipping tiers leaked into the file.
    forbes_note = "Forbes: not built"
    forbes_path = WEB / "forbes.json"
    if forbes_path.exists():
        forbes = read("forbes.json")
        assert forbes["advisors"], "forbes.json carries no advisors"
        entries = [e for rows in forbes["advisors"].values() for e in rows]
        assert all(len(e) == 5 for e in entries), "forbes entry is not 5 wide"
        assert all(e[3] in ("c", "h") for e in entries),             "forbes entry ships a tier below 'high' -- review rows must be withheld"
        matched = sum(1 for crd in forbes["advisors"] if crd in advisor_ids)
        assert matched == len(forbes["advisors"]),             "forbes.json names a CRD that is not a mapped advisor"
        confirmed = sum(1 for e in entries if e[3] == "c")
        forbes_note = (f"Forbes: {len(forbes['advisors']):,} advisors "
                       f"({confirmed:,} confirmed, {len(entries) - confirmed:,} inferred); "
                       f"team assets for {len(forbes['team_assets']):,}")

    print(barrons_note + ".")
    print(forbes_note + ".")


if __name__ == "__main__":
    main()
