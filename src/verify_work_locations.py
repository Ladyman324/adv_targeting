"""Fail release if a chosen working location disappeared from a map shard.

Run after all state pins and export_national.py have been rebuilt.
"""
from __future__ import annotations

import collections
import json
import pathlib

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = ROOT / "webapp/data"
INTERIM = ROOT / "data/interim"


def main() -> None:
    chosen = pd.read_parquet(INTERIM / "advisor_placement.parquet")
    expected = {(str(a), str(f)): (str(key), bool(unc))
                for a, f, key, unc in chosen[[
                    "advisor_crd", "firm_crd", "addr_key", "uncertain"
                ]].itertuples(index=False, name=None)}
    work = pd.read_parquet(INTERIM / "contact_work_branches.parquet")
    source = {(str(a), str(f)): (str(state), str(kind))
              for a, f, state, kind in work[[
                  "advisor_crd", "firm_crd", "branch_state", "location_source"
              ]].itertuples(index=False, name=None)}
    actual = collections.defaultdict(list)
    uncertain = 0
    for path in sorted(WEB.glob("pins_??.json")):
        state = path.stem[-2:]
        data = json.loads(path.read_text(encoding="utf-8"))
        for pin in data["pins"]:
            pair = (str(pin[6]), str(data["firms"][pin[2]][7]))
            street = str(data["addrs"][pin[3]]).strip().upper()
            city = str(data["cities"][pin[4]]).strip().upper()
            postal = str(pin[5])[:5]
            actual[pair].append((state, f"{street}|{city}|{postal}",
                                 bool(pin[14]), int(pin[18]) if len(pin) > 18 else 0))
            uncertain += bool(pin[14])
    missing = set(expected) - set(actual)
    extra = set(actual) - set(expected)
    duplicate = [pair for pair, pins in actual.items() if len(pins) != 1]
    wrong_key = [pair for pair, pins in actual.items()
                 if pair in source and pair in expected
                 and pins[0][1] != expected[pair][0]]
    wrong_source = [
        pair for pair, (state, kind) in source.items()
        if pair not in actual or actual[pair][0][0] != state
        or actual[pair][0][3] != {"firm_roster": 1, "ACT": 2}[kind]
        or actual[pair][0][2]]
    print(f"placements {len(expected):,}; pins {sum(map(len, actual.values())):,}; "
          f"source-backed {len(source):,}; uncertain {uncertain:,}")
    if missing or extra or duplicate or wrong_key or wrong_source:
        raise RuntimeError(
            f"map integrity failed: missing {len(missing)}, extra {len(extra)}, "
            f"duplicate {len(duplicate)}, address-key mismatch {len(wrong_key)}, "
            f"source mismatch {len(wrong_source)}; samples "
            f"{list(missing)[:2]} {wrong_key[:2]} {wrong_source[:2]}")
    pair = ("7320366", "250")
    if pair in source and actual[pair][0][0] != "NY":
        raise RuntimeError("Aaron Fayzulayev did not move to New York")
    print("working-location pin integrity passed")


if __name__ == "__main__":
    main()
