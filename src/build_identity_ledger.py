"""Build the versioned Act/SEC identity evidence ledger from Act API JSON.

This pipeline deliberately has no Excel input.  The legacy crosswalk is
consulted only for review suggestions on Act records without an asserted CRD;
it can never create an automatic approval.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import sys
from collections import defaultdict

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from identity_normalize import clean_text, normalize_crd, normalize_email, name_tokens
from identity_resolver import apply_decision, evaluate_assertion, prepare_act_records
from contact_provenance import ProvenanceError, validate_owner_artifact
from identity_schema import (
    DECISIONS_FILENAME, EVIDENCE_COLUMNS, EVIDENCE_FILENAME, IDENTITY_DIRNAME,
    LINKS_FILENAME, LINK_COLUMNS, MANIFEST_FILENAME, RULESET_VERSION,
    SOURCE_RECORD_COLUMNS, SOURCE_RECORDS_FILENAME, content_hash,
)

RAW, INTERIM = ROOT / "data" / "raw", ROOT / "data" / "interim"
OUT = ROOT / "data" / IDENTITY_DIRNAME


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_input(payload: dict, role: str) -> dict:
    matches = [item for item in payload.get("inputs", [])
               if item.get("role") == role]
    if len(matches) != 1:
        raise SystemExit(f"ACT pull manifest must contain exactly one {role} input")
    return matches[0]


def select_act_pull(raw_dir: pathlib.Path = RAW) -> tuple[pathlib.Path, pathlib.Path,
                                                          pathlib.Path, dict]:
    """Select the newest committed ACT pair and verify every claimed fact.

    The manifest is the commit marker. A bare, newer contacts JSON is an
    interrupted/orphan pull, not a candidate downstream code may consume.
    """
    manifests = sorted(raw_dir.glob("act_pull_manifest_*.json"))
    if not manifests:
        raise SystemExit("No committed data/raw/act_pull_manifest_*.json exists")
    manifest_path = manifests[-1]
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid ACT pull manifest {manifest_path}: {exc}") from exc
    policies = payload.get("policies", {})
    pull_id = clean_text(policies.get("pull_id"))
    if (payload.get("schema_version") != 1 or not pull_id or
            policies.get("pair_complete") is not True or
            policies.get("partial_refreshes") != "forbidden"):
        raise SystemExit(f"Incomplete ACT pull manifest: {manifest_path}")
    contacts_fact = _manifest_input(payload, "act_contacts")
    owner_fact = _manifest_input(payload, "act_eic_contact")
    expected_names = {
        "act_contacts": f"act_contacts_{pull_id}.json",
        "act_eic_contact": f"act_eic_contact_{pull_id}.json",
    }
    selected = {}
    for role, fact in (("act_contacts", contacts_fact),
                       ("act_eic_contact", owner_fact)):
        name = pathlib.Path(clean_text(fact.get("file"))).name
        if name != expected_names[role] or fact.get("complete") is not True:
            raise SystemExit(f"ACT pull manifest has an invalid {role} artifact")
        path = raw_dir / name
        if not path.is_file():
            raise SystemExit(f"ACT pull artifact is missing: {path}")
        if int(fact.get("bytes") or -1) != path.stat().st_size:
            raise SystemExit(f"ACT pull artifact byte count changed: {path}")
        if clean_text(fact.get("sha256")) != sha256_file(path):
            raise SystemExit(f"ACT pull artifact hash changed: {path}")
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Invalid ACT pull artifact {path}: {exc}") from exc
        if role == "act_contacts":
            actual_rows = len(rows) if isinstance(rows, list) else -1
        else:
            owners = rows.get("owner_by_contact_id") if isinstance(rows, dict) else None
            actual_rows = len(owners) if isinstance(owners, dict) else -1
        if int(fact.get("rows") or -1) != actual_rows:
            raise SystemExit(f"ACT pull artifact row count changed: {path}")
        selected[role] = path
    try:
        validate_owner_artifact(selected["act_contacts"],
                                selected["act_eic_contact"])
    except ProvenanceError as exc:
        raise SystemExit(f"ACT pull owner pair is invalid: {exc}") from exc
    newer_contacts = [path.name for path in raw_dir.glob("act_contacts_*.json")
                      if path.name > selected["act_contacts"].name]
    if newer_contacts:
        raise SystemExit(
            "Refusing orphan/newer ACT contacts without a complete paired "
            f"manifest: {', '.join(sorted(newer_contacts))}")
    return (selected["act_contacts"], selected["act_eic_contact"],
            manifest_path, payload)


def records_by_crd(frame: pd.DataFrame) -> dict[str, dict]:
    out = {}
    for row in frame.fillna("").to_dict("records"):
        crd = normalize_crd(row.get("advisor_crd"))
        if crd and crd not in out:
            out[crd] = row
    return out


def sec_context() -> tuple[dict, dict, dict, dict]:
    advisors = pd.read_parquet(INTERIM / "advisors.parquet",
                               columns=["advisor_crd", "first_name", "middle_name",
                                        "last_name", "suffix", "used_first_name"])
    sec = records_by_crd(advisors)
    current, prior, branches = defaultdict(lambda: {
        "firm_crds": set(), "firm_names": set()}), defaultdict(set), defaultdict(list)
    emp = pd.read_parquet(INTERIM / "advisor_employments.parquet",
                          columns=["advisor_crd", "firm_crd",
                                   "firm_name_on_record"]).fillna("")
    for row in emp.to_dict("records"):
        crd = normalize_crd(row["advisor_crd"])
        if crd:
            current[crd]["firm_crds"].add(clean_text(row["firm_crd"]))
            current[crd]["firm_names"].add(clean_text(row["firm_name_on_record"]))
    hist = pd.read_parquet(INTERIM / "advisor_employment_history.parquet",
                           columns=["advisor_crd", "firm_name_on_record"]).fillna("")
    for row in hist.to_dict("records"):
        crd = normalize_crd(row["advisor_crd"])
        if crd:
            prior[crd].add(clean_text(row["firm_name_on_record"]))
    branch = pd.read_parquet(
        INTERIM / "advisor_branches.parquet",
        columns=["advisor_crd", "branch_street1", "branch_city",
                 "branch_state", "branch_postal"]).fillna("")
    for row in branch.to_dict("records"):
        crd = normalize_crd(row["advisor_crd"])
        item = {"street": clean_text(row["branch_street1"]),
                "city": clean_text(row["branch_city"]),
                "state": clean_text(row["branch_state"]).upper(),
                "postal": clean_text(row["branch_postal"])}
        if crd and item not in branches[crd]:
            branches[crd].append(item)
    return sec, current, prior, branches


def crosswalk_suggestions(source_name: str) -> dict[str, dict]:
    path = INTERIM / "act_crosswalk.parquet"
    if not path.exists():
        return {}
    frame = pd.read_parquet(path).fillna("")
    if "source_file" in frame:
        frame = frame[frame["source_file"].astype(str) == source_name]
    out = {}
    for row in frame.to_dict("records"):
        act_id, crd = clean_text(row.get("act_id")), normalize_crd(row.get("advisor_crd"))
        tier = clean_text(row.get("tier")).lower()
        if act_id and crd and tier in {"confirmed", "high", "review"}:
            out[act_id] = {"advisor_crd": crd, "tier": tier,
                           "match_score": float(row.get("match_score") or 0),
                           "match_gap": float(row.get("match_gap") or 0),
                           "source_file": clean_text(row.get("source_file"))}
    return out


def independent_roster_evidence() -> dict[str, list[dict]]:
    """Index scraped firm-roster rows by exact email with explicit provenance."""
    from build_contacts import load_rosters
    frame = load_rosters().fillna("")
    out = defaultdict(list)
    for row in frame.to_dict("records"):
        email = normalize_email(row.get("email"))
        if not email:
            continue
        tokens = name_tokens(row.get("name"))
        item = {
            "source": clean_text(row.get("source")),
            "source_file": clean_text(row.get("source_file")),
            "name": clean_text(row.get("name")),
            "name_first": tokens[0] if tokens else "",
            "name_last": tokens[-1] if len(tokens) > 1 else "",
            "phone": clean_text(row.get("phone")),
            "firm_crd": normalize_crd(row.get("firm_crd")),
            "city": clean_text(row.get("city")),
            "state": clean_text(row.get("state")).upper(),
        }
        if item not in out[email]:
            out[email].append(item)
    return dict(out)


def load_decisions() -> tuple[dict[str, dict], str]:
    path = OUT / DECISIONS_FILENAME
    if not path.exists():
        return {}, ""
    payload = json.loads(path.read_text(encoding="utf-8"))
    decisions = payload.get("decisions", {})
    expected = content_hash({"schemaVersion": payload.get("schemaVersion", 1),
                             "decisions": decisions})
    if payload.get("contentHash") != expected:
        raise SystemExit(f"{path} has an invalid contentHash; refusing decisions")
    return {clean_text(k): v for k, v in decisions.items()}, expected


def enforce_final_uniqueness(links: list[dict]) -> None:
    """Fail closed after overrides: approved CRDs and emails remain one-to-one."""
    by_crd, by_email = defaultdict(list), defaultdict(list)
    for link in links:
        if link["identity_status"] == "approved":
            by_crd[link["advisor_crd"]].append(link)
            if link["email"]:
                by_email[link["email"]].append(link)
    collisions = set()
    for group in list(by_crd.values()) + list(by_email.values()):
        if len(group) > 1:
            collisions.update(id(row) for row in group)
    for link in links:
        if id(link) in collisions:
            reasons = json.loads(link["hard_conflicts_json"])
            reasons.append("post_decision_identity_collision")
            link.update({
                "identity_status": "quarantine",
                "decision_reason": "post_decision_identity_collision",
                "hard_conflicts_json": json.dumps(sorted(set(reasons)),
                                                   separators=(",", ":")),
                "can_display_contact": False, "can_call": False,
                "can_email": False, "can_sync_act": False,
            })


def atomic_json(path: pathlib.Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-rosters", action="store_true",
                        help="skip independent roster evidence (diagnostic only)")
    args = parser.parse_args()
    source, owner_source, pull_manifest_path, pull_manifest = select_act_pull()
    source_hash = sha256_file(source)
    print(f"[*] Act JSON: {source.name} ({source_hash[:12]})")
    rows = json.loads(source.read_text(encoding="utf-8"))
    records = prepare_act_records(rows, source.name, source_hash)
    del rows
    sec, current, prior, branches = sec_context()
    suggestions = crosswalk_suggestions(source.name)
    rosters = {} if args.no_rosters else independent_roster_evidence()
    roster_sources = ({p.name: sha256_file(p) for p in
                       sorted((RAW / "firm_rosters").glob("*")) if p.is_file()}
                      if not args.no_rosters else {})
    decisions, decisions_hash = load_decisions()
    evidence_rows, links = [], []
    for record in records:
        act_id = record["source_record_id"]
        claimed = normalize_crd(record["raw_claimed_crd"])
        candidate = suggestions.get(act_id, {}) if not claimed else {}
        target = claimed or normalize_crd(candidate.get("advisor_crd"))
        cur = current.get(target, {"firm_crds": set(), "firm_names": set()})
        context = {
            "candidate": candidate,
            "current_firms": sorted(cur["firm_crds"]),
            "current_firm_names": sorted(cur["firm_names"]),
            "prior_firm_names": sorted(prior.get(target, set())),
            "branches": branches.get(target, []),
            "roster": rosters.get(record["norm_email"], []),
        }
        evidence, link = evaluate_assertion(record, sec.get(target), context)
        if act_id in decisions:
            link = apply_decision(link, decisions[act_id], sec)
        evidence_rows.append(evidence)
        links.append(link)
    enforce_final_uniqueness(links)
    OUT.mkdir(parents=True, exist_ok=True)
    source_records_path = OUT / SOURCE_RECORDS_FILENAME
    evidence_path = OUT / EVIDENCE_FILENAME
    links_path = OUT / LINKS_FILENAME
    pd.DataFrame(records, columns=SOURCE_RECORD_COLUMNS).to_parquet(
        source_records_path, index=False)
    pd.DataFrame(evidence_rows, columns=EVIDENCE_COLUMNS).to_parquet(
        evidence_path, index=False)
    pd.DataFrame(links, columns=LINK_COLUMNS).to_parquet(
        links_path, index=False)
    status_counts = pd.Series([r["identity_status"] for r in links]).value_counts().to_dict()
    manifest_core = {
        "schemaVersion": 1, "rulesetVersion": RULESET_VERSION,
        "actSource": {"file": source.name, "sha256": source_hash,
                      "rows": len(records)},
        "actPull": {
            "manifest": pull_manifest_path.name,
            "manifestSha256": sha256_file(pull_manifest_path),
            "pullId": pull_manifest["policies"]["pull_id"],
            "ownerFile": owner_source.name,
            "ownerSha256": sha256_file(owner_source),
        },
        "secSources": {p.name: sha256_file(p) for p in (
            INTERIM / "advisors.parquet", INTERIM / "advisor_branches.parquet",
            INTERIM / "advisor_employments.parquet",
            INTERIM / "advisor_employment_history.parquet")},
        "crosswalkSource": {
            "file": "act_crosswalk.parquet",
            "sha256": sha256_file(INTERIM / "act_crosswalk.parquet"),
        },
        "rosterEvidence": not args.no_rosters,
        "rosterSources": roster_sources,
        "decisionsHash": decisions_hash, "statusCounts": status_counts,
        "outputs": {
            SOURCE_RECORDS_FILENAME: {"sha256": sha256_file(source_records_path),
                                      "rows": len(records)},
            EVIDENCE_FILENAME: {"sha256": sha256_file(evidence_path),
                                "rows": len(evidence_rows)},
            LINKS_FILENAME: {"sha256": sha256_file(links_path),
                             "rows": len(links)},
        },
    }
    manifest = {**manifest_core,
                "generatedUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "contentHash": content_hash(manifest_core)}
    atomic_json(OUT / MANIFEST_FILENAME, manifest)
    print(f"[+] {OUT / LINKS_FILENAME}: {len(links):,} links {status_counts}")
    print(f"[+] Evidence is deterministic; build time is manifest metadata only.")


if __name__ == "__main__":
    main()
