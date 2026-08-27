"""Validate colleague decisions; dry-run by default, --apply writes the ledger."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
from identity_normalize import clean_text, normalize_crd
from identity_schema import (
    ACT_RECORD_ACTIONS, CORRECTIONS_XLSX_FILENAME, DECISIONS_FILENAME,
    EVIDENCE_FILENAME, IDENTITY_DIRNAME, LINK_DECISIONS, PREFERRED_DECISIONS,
    content_hash,
)

IDENTITY = ROOT / "data" / IDENTITY_DIRNAME


def truth(value) -> bool:
    return clean_text(value).lower() in {"1", "true", "yes", "y"}


def read_review(path: pathlib.Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        return pd.read_excel(path, sheet_name="Review", dtype=str).fillna("")
    return pd.read_csv(path, dtype=str).fillna("")


def normalize_decision(row: dict, now: str) -> dict:
    decision = {
        "act_id": clean_text(row.get("act_id")),
        "expected_evidence_hash": clean_text(row.get("expected_evidence_hash")),
        "link_decision": clean_text(row.get("link_decision")).lower(),
        "resolved_crd": normalize_crd(row.get("resolved_crd")),
        "canonical_for_crd": truth(row.get("canonical_for_crd")),
        "act_record_action": clean_text(row.get("act_record_action")).lower(),
        "preferred_decision": clean_text(row.get("preferred_decision")).lower(),
        "preferred_first": clean_text(row.get("preferred_first")),
        "reviewer": clean_text(row.get("reviewer")),
        "reviewed_utc": clean_text(row.get("reviewed_utc")) or now,
        "reason_code": clean_text(row.get("reason_code")),
        "notes": clean_text(row.get("notes")),
    }
    decision["decision_hash"] = content_hash({
        "schemaVersion": 1, "decision": decision})
    return decision


def load_existing_decisions(path: pathlib.Path) -> dict:
    """Read only a content-hash-valid decision store before merging into it."""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid existing decisions file {path}: {exc}") from exc
    decisions = payload.get("decisions")
    if payload.get("schemaVersion") != 1 or not isinstance(decisions, dict):
        raise SystemExit(f"Invalid existing decisions schema: {path}")
    core = {"schemaVersion": 1, "decisions": decisions}
    if payload.get("contentHash") != content_hash(core):
        raise SystemExit(
            f"Existing decisions contentHash is invalid; refusing merge: {path}")
    return decisions


def validate(decision: dict, evidence_by_act: dict, sec_crds: set) -> list[str]:
    errors = []
    act_id = decision["act_id"]
    if not act_id or act_id not in evidence_by_act:
        errors.append("unknown_act_id")
    elif decision["expected_evidence_hash"] != evidence_by_act[act_id]:
        errors.append("stale_evidence_hash")
    if decision["link_decision"] not in LINK_DECISIONS | {""}:
        errors.append("invalid_link_decision")
    if decision["preferred_decision"] not in PREFERRED_DECISIONS:
        errors.append("invalid_preferred_decision")
    if decision["act_record_action"] not in ACT_RECORD_ACTIONS:
        errors.append("invalid_act_record_action")
    if decision["link_decision"] in {"approve", "replace"}:
        if not decision["resolved_crd"] or decision["resolved_crd"] not in sec_crds:
            errors.append("resolved_crd_not_in_sec")
    if decision["preferred_decision"] == "approve" and not decision["preferred_first"]:
        errors.append("preferred_first_required")
    if not decision["link_decision"] and not decision["preferred_decision"] and not decision["act_record_action"]:
        errors.append("empty_decision")
    if not decision["reviewer"]:
        errors.append("reviewer_required")
    if not decision["reason_code"]:
        errors.append("reason_code_required")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", nargs="?",
                        default=str(IDENTITY / CORRECTIONS_XLSX_FILENAME))
    parser.add_argument("--apply", action="store_true",
                        help="write validated decisions; default is dry-run")
    args = parser.parse_args()
    path = pathlib.Path(args.input)
    frame = read_review(path)
    selected = frame[
        frame.get("link_decision", "").astype(str).str.strip().ne("") |
        frame.get("preferred_decision", "").astype(str).str.strip().ne("") |
        frame.get("act_record_action", "").astype(str).str.strip().ne("")]
    evidence = pd.read_parquet(
        IDENTITY / EVIDENCE_FILENAME,
        columns=["source_record_id", "evidence_hash"]).fillna("")
    evidence_by_act = dict(zip(evidence["source_record_id"],
                               evidence["evidence_hash"]))
    advisors = pd.read_parquet(
        ROOT / "data" / "interim" / "advisors.parquet",
        columns=["advisor_crd"])
    sec_crds = {normalize_crd(v) for v in advisors["advisor_crd"]}
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    accepted, rejected = {}, []
    for number, row in selected.iterrows():
        decision = normalize_decision(row.to_dict(), now)
        errors = validate(decision, evidence_by_act, sec_crds)
        if errors:
            rejected.append((number + 2, decision["act_id"], errors))
        elif decision["act_id"] in accepted:
            rejected.append((number + 2, decision["act_id"],
                             ["duplicate_decision_in_input"]))
        else:
            accepted[decision["act_id"]] = decision
    for row_no, act_id, errors in rejected:
        print(f"[!] row {row_no} Act {act_id or '<blank>'}: {', '.join(errors)}")
    print(f"[*] {len(accepted)} valid decision(s); {len(rejected)} rejected")
    if rejected:
        raise SystemExit("No decisions written: correct every rejected row.")
    if not args.apply:
        print("[dry-run] Nothing written. Re-run with --apply after reviewing.")
        return
    out = IDENTITY / DECISIONS_FILENAME
    existing = load_existing_decisions(out)
    existing.update(accepted)
    core = {"schemaVersion": 1, "decisions": dict(sorted(existing.items()))}
    payload = {**core, "contentHash": content_hash(core)}
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")), encoding="utf-8")
    tmp.replace(out)
    print(f"[+] {out}: {len(existing)} hash-bound decision(s)")
    print("[!] Rebuild the identity ledger and report; this does not write Act.")


if __name__ == "__main__":
    main()
