"""Reduce pinned firm API/ownership caches offline, without claiming retrieval."""
import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from fetch_firm_names import other_names
from fetch_firm_owners import OWNERSHIP_BANDS, parse_rows

OWNER_COLUMNS = ["firm_crd", "schedule", "owner_name", "owner_type", "owned_entity",
                 "title_or_status", "date_acquired", "ownership_code", "control_person",
                 "public_reporting", "owner_crd", "ownership_band"]


def _prior(root, name, columns):
    for directory in ("data/interim", "data/output"):
        path = root / directory / (name + ".parquet")
        if path.exists():
            frame = pd.read_parquet(path)
            if not set(columns).issubset(frame.columns):
                raise ValueError("Invalid prior firm reference schema: " + name)
            frame["firm_crd"] = frame.firm_crd.astype(str)
            return frame.reindex(columns=columns)
    return pd.DataFrame(columns=columns)


def reduce_firm_reference(stage_root):
    """Return coverage/reuse evidence; write derived parquet and reduction receipt.

    Cache dates are not retrieval dates. Missing cache retains prior per-firm
    evidence explicitly. A failed cached owner retrieval cannot erase owners.
    """
    root = Path(stage_root)
    universe_path = root / "data/interim/firms_scored.parquet"
    if not universe_path.exists():
        universe_path = root / "data/output/firms.parquet"
    firms = set(pd.read_parquet(universe_path, columns=["crd"]).crd.astype(str))
    receipt = root / "data/interim/firm_reference_refresh.json"
    previous = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else {}
    summary = {"schemaVersion": 1, "network": False, "retrieved": False, "sources": {}}
    outputs = []
    for directory, name, columns in (("firm_api", "firm_other_names", ["firm_crd", "other_name"]),
                                     ("firm_owners", "firm_owners", OWNER_COLUMNS)):
        prior = _prior(root, name, columns)
        prior = prior[prior.firm_crd.isin(firms)]
        rows, replaced, failures, hashes = [], set(), [], {}
        paths = sorted((root / "data" / directory).glob("*.json"))
        for path in paths:
            crd = path.stem
            if not crd.isdigit():
                raise ValueError("Firm cache filename must be its numeric CRD")
            content = path.read_bytes()
            hashes[path.name] = hashlib.sha256(content).hexdigest()
            record = json.loads(content.decode("utf-8-sig"))
            if not isinstance(record, dict):
                raise ValueError("Firm cache must contain an object: " + path.name)
            if crd not in firms:
                continue
            if name == "firm_other_names":
                basic = record.get("basicInformation")
                if not isinstance(basic, dict) or not isinstance(basic.get("otherNames", []), list):
                    raise ValueError("Cannot derive aliases from pinned cache: " + path.name)
                if any(not isinstance(value, str) for value in basic.get("otherNames", [])):
                    raise ValueError("Firm aliases must be strings: " + path.name)
                declared = basic.get("firmId", basic.get("firmCrdNb"))
                if declared is not None and str(declared) != crd:
                    raise ValueError("Firm alias cache CRD disagrees with filename")
                rows.extend({"firm_crd": crd, "other_name": value} for value in other_names(record))
            else:
                if str(record.get("crd", crd)) != crd or type(record.get("ok")) is not bool:
                    raise ValueError("Invalid ownership cache identity/status: " + path.name)
                if not record["ok"]:
                    old_hash = previous.get("sources", {}).get(name, {}).get("cacheHashes", {}).get(path.name)
                    if old_hash and old_hash != hashes[path.name]:
                        raise ValueError("Changed ownership cache cannot be reduced: " + path.name)
                    failures.append(crd)
                    continue
                for schedule in ("A", "B"):
                    values = record.get(schedule)
                    if isinstance(values, str):
                        if "FULL LEGAL NAME" not in values.upper():
                            raise ValueError("Ownership HTML lacks schedule header")
                        values = parse_rows(values)
                    if not isinstance(values, list) or any(not isinstance(value, dict) or not value.get("owner_name") for value in values):
                        raise ValueError("Invalid ownership rows: " + path.name)
                    for value in values:
                        row = {key: str(value.get(key, "") or "") for key in OWNER_COLUMNS}
                        row.update(firm_crd=crd, schedule=schedule,
                                   ownership_band=OWNERSHIP_BANDS.get(value.get("ownership_code"), ""))
                        rows.append(row)
            replaced.add(crd)
        retained = prior[~prior.firm_crd.isin(replaced)]
        frame = pd.concat([retained, pd.DataFrame(rows, columns=columns)], ignore_index=True)
        frame = frame.drop_duplicates().sort_values(columns, kind="stable").reset_index(drop=True)
        status = "rebuilt_from_pinned_cache" if replaced else "reused_derived" if len(prior) else "unavailable"
        summary["sources"][name] = {"status": status, "rows": len(frame), "cachedFirms": len(replaced),
            "reusedFirms": int(retained.firm_crd.nunique()), "unavailableCachedFirms": failures,
            "missingCacheFirms": len(firms - replaced - set(failures)), "cacheHashes": hashes,
            "retrieved": False}
        outputs.append((name, frame))
    # Validate every cache before replacing any derived data.
    for name, frame in outputs:
        for directory in ("data/interim", "data/output"):
            target = root / directory / (name + ".parquet")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".parquet.tmp")
            frame.to_parquet(temporary, index=False)
            temporary.replace(target)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    temp = receipt.with_suffix(".json.tmp")
    temp.write_text(json.dumps(summary, sort_keys=True, indent=2), encoding="utf-8")
    temp.replace(receipt)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    print(json.dumps(reduce_firm_reference(parser.parse_args().root), sort_keys=True))
