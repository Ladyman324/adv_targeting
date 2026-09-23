"""Fail a release when search or field shards refer to stale map locations."""
from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = ROOT / "webapp" / "data"


def read(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def verify(root: pathlib.Path = WEB) -> None:
    index = read(root / "advisor_index.json")
    canonical = {str(row[0]): row[:7] for row in index["advisors"]}
    manifest = read(root / "advisor_search.json")
    if (manifest["advisors"] != len(canonical)
            or manifest["firms"] != index["firms"]
            or manifest["cities"] != index["cities"]):
        raise RuntimeError("desktop search manifest differs from advisor_index.json")

    search_dir = root / "search"
    expected_paths = {
        *(search_dir / f"{prefix}.json" for prefix in manifest["shards"]),
        *(search_dir / "crd" / f"{prefix}.json" for prefix in manifest["crdShards"]),
    }
    actual_paths = set(search_dir.rglob("*.json"))
    if actual_paths != expected_paths:
        raise RuntimeError(
            f"desktop search shard files differ from manifest: "
            f"{len(expected_paths - actual_paths)} missing, "
            f"{len(actual_paths - expected_paths)} stale")
    crd_seen = set()
    for path in sorted(expected_paths):
        is_crd = path.parent.name == "crd"
        for row in read(path)["rows"]:
            crd = str(row[0])
            if len(row) != 8 or row[:7] != canonical.get(crd):
                raise RuntimeError(f"{path}: stale search row for CRD {crd}")
            if is_crd:
                if crd in crd_seen:
                    raise RuntimeError(f"{path}: duplicate CRD {crd}")
                crd_seen.add(crd)
    if crd_seen != set(canonical):
        raise RuntimeError(
            f"desktop CRD search is missing {len(set(canonical) - crd_seen)} advisors")

    # A rebuilt desktop index alone is insufficient: the Field App has its own
    # tiles and name shards, and a stale tile can send a rep to another state.
    pin_states: dict[str, set[str]] = {}
    for path in root.glob("pins_??.json"):
        state = path.stem[-2:]
        for pin in read(path)["pins"]:
            pin_states.setdefault(str(pin[6]), set()).add(state)
    tile_columns = read(root / "tile_index.json")["columns"]
    col = {name: n for n, name in enumerate(tile_columns)}
    field_rows = {}
    for path in (root / "tiles").glob("*.json"):
        tile = read(path)
        for row in tile["rows"]:
            crd = str(row[col["crd"]])
            state = row[col["state"]]
            if state not in pin_states.get(crd, set()):
                raise RuntimeError(f"{path}: field tile for CRD {crd} has no {state} pin")
            if crd in field_rows:
                raise RuntimeError(f"{path}: duplicate field tile for CRD {crd}")
            field_rows[crd] = [row[col["name"]], crd, tile["cell"],
                               row[col["city"]], state]
    names = read(root / "name_index.json")
    names_dir = root / "names"
    expected_names = {names_dir / f"{prefix}.json" for prefix in names["shards"]}
    actual_names = set(names_dir.glob("*.json"))
    if actual_names != expected_names:
        raise RuntimeError("field name shard files differ from manifest")
    name_seen = set()
    for path in sorted(expected_names):
        for row in read(path)["rows"]:
            crd = str(row[1])
            if len(row) != 6 or row[:5] != field_rows.get(crd):
                raise RuntimeError(f"{path}: stale field search row for CRD {crd}")
            name_seen.add(crd)
    if name_seen != set(field_rows):
        raise RuntimeError(
            f"field search is missing {len(set(field_rows) - name_seen)} advisors")
    print(f"search shards agree with national index and state pins: "
          f"{len(canonical):,} desktop advisors, {len(field_rows):,} field advisors")


if __name__ == "__main__":
    verify()
