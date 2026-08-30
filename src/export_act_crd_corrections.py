"""Export a colleague-friendly, hash-bound Act CRD correction workbook and CSV."""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
from identity_schema import (
    CORRECTIONS_CSV_FILENAME, CORRECTIONS_XLSX_FILENAME, EVIDENCE_FILENAME,
    IDENTITY_DIRNAME, LINKS_FILENAME, MANIFEST_FILENAME,
    SOURCE_RECORDS_FILENAME, content_hash,
)
from contact_provenance import sha256_file

IDENTITY = ROOT / "data" / IDENTITY_DIRNAME

# These records are deliberate workflow sentinels, not a convenient sample.
# They must survive candidate ranking/caps so the correction workbook remains
# reproducible when it is rebuilt with the documented default command.
REQUIRED_ACT_IDS = frozenset({
    "855992fd-e032-4454-947b-3dcd5515dfe3",  # Chris Tolman / UBS
})

REVIEW_COLUMNS = [
    "priority", "issue_type", "recommended_action", "identity_status",
    "decision_reason", "act_id",
    "current_crd", "proposed_crd", "act_full_name", "act_structured_name",
    "act_salutation", "act_company", "act_city", "act_state", "act_postal",
    "act_email", "act_email_domain", "expected_sec_firm_crds",
    "actual_sec_firm_crds", "sec_name", "sec_used_first", "candidate_tier",
    "candidate_score", "candidate_gap", "positive_evidence", "hard_conflicts",
    "warnings", "roster_evidence", "expected_evidence_hash", "link_decision",
    "identity_manifest_hash", "act_source_file", "act_source_sha256",
    "resolved_crd", "canonical_for_crd", "act_record_action",
    "preferred_decision", "preferred_first", "reviewer", "reviewed_utc",
    "reason_code", "notes",
]


def report_email_domain(row) -> str:
    """Read the domain across current and pre-suffix ledger schemas."""
    return (row.get("email_domain_ev", "") or
            row.get("email_domain_act", "") or
            row.get("email_domain", ""))


def claimed_domain_conflict_mask(rows: pd.DataFrame) -> pd.Series:
    """Only an actual ACT CRD can be an ACT CRD/domain correction."""
    claimed = rows["claimed_crd"].astype(str).str.strip().ne("")
    conflicts = rows["email_domain_current_conflicts"].astype(bool)
    return claimed & conflicts


def is_claimed_domain_conflict(row) -> bool:
    """Scalar form also used after manual sentinel/pinned-row selection."""
    return bool(str(row.get("claimed_crd", "") or "").strip() and
                row.get("email_domain_current_conflicts"))


def validate_identity_manifest(identity: pathlib.Path = IDENTITY) -> dict:
    """Refuse a mixed or edited ledger before asking a colleague to review it."""
    manifest_path = identity / MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid identity manifest {manifest_path}: {exc}") from exc
    core = {key: value for key, value in manifest.items()
            if key not in {"generatedUtc", "contentHash"}}
    if manifest.get("contentHash") != content_hash(core):
        raise SystemExit(f"Identity manifest content hash is invalid: {manifest_path}")
    outputs = manifest.get("outputs") or {}
    for filename in (SOURCE_RECORDS_FILENAME, EVIDENCE_FILENAME, LINKS_FILENAME):
        path, fact = identity / filename, outputs.get(filename) or {}
        if not path.is_file():
            raise SystemExit(f"Identity artifact is missing: {path}")
        if fact.get("sha256") != sha256_file(path):
            raise SystemExit(f"Identity artifact hash differs from manifest: {path}")
    return manifest


def build_review_frame(candidate_limit: int = 250,
                       include_act_ids=()) -> pd.DataFrame:
    manifest = validate_identity_manifest()
    source = pd.read_parquet(IDENTITY / SOURCE_RECORDS_FILENAME).fillna("")
    evidence = pd.read_parquet(IDENTITY / EVIDENCE_FILENAME).fillna("")
    links = pd.read_parquet(IDENTITY / LINKS_FILENAME).fillna("")
    rows = source.merge(evidence, on=["record_key", "source_record_id"],
                        suffixes=("_act", "_ev"), validate="one_to_one").merge(
        links, on=["record_key", "source_record_id"], suffixes=("", "_link"),
        validate="one_to_one")
    problems = rows[rows["identity_status"].isin(["quarantine", "rejected"])].copy()
    domain_conflict_mask = claimed_domain_conflict_mask(rows)
    domain_conflicts = rows[domain_conflict_mask].copy()
    preferred = rows[(rows["identity_status"] == "approved") &
                     (rows["preferred_status"] == "review")].copy()
    candidates = rows[(rows["identity_status"] == "unmatched") &
                      rows["candidate_tier"].isin(["confirmed", "high"]) &
                      ~rows["email_domain_current_conflicts"].astype(bool)].copy()
    candidates = candidates.sort_values(
        ["candidate_tier", "candidate_score", "candidate_gap"],
        ascending=[True, False, False]).head(candidate_limit)
    pinned_ids = REQUIRED_ACT_IDS | {
        str(act_id).strip() for act_id in include_act_ids if str(act_id).strip()
    }
    pinned = rows[rows["source_record_id"].isin(pinned_ids)].copy()
    picked = pd.concat([problems, domain_conflicts, preferred, candidates, pinned],
                       ignore_index=True).drop_duplicates(
                           subset=["source_record_id"], keep="first")

    def report_row(row):
        if is_claimed_domain_conflict(row):
            issue, priority = "act_crd_firm_conflict", 1
            recommended = ("Verify whether the person moved firms. Correct or clear the "
                           "ACT CRD if the roster-domain firm family is current.")
        elif row["identity_status"] in {"quarantine", "rejected"}:
            issue, priority = "existing_crd_correction", 1
            recommended = "Verify the CRD and correct, replace, or clear it in ACT."
        elif row["preferred_status"] == "review":
            issue, priority = "preferred_name_review", 2
            recommended = "Confirm the preferred greeting name."
        else:
            issue, priority = "candidate_crd_addition", 3
            recommended = "Verify the proposed CRD before adding it to ACT."
        sec_full = " ".join(x for x in (
            row["sec_first_name"], row["sec_middle_name"], row["sec_last_name"],
            row["sec_suffix"]) if x)
        final_crd = row.get("advisor_crd_link", "")
        proposed = row["candidate_crd"]
        if not proposed or proposed == row["claimed_crd"]:
            proposed = final_crd if final_crd != row["claimed_crd"] else ""
        return {
            "priority": priority, "issue_type": issue,
            "recommended_action": recommended,
            "identity_status": row["identity_status"],
            "decision_reason": row["decision_reason"],
            "act_id": row["source_record_id"], "current_crd": row["claimed_crd"],
            "proposed_crd": proposed,
            "act_full_name": row["raw_full_name"],
            "act_structured_name": " ".join(x for x in (
                row["raw_first_name"], row["raw_middle_name"],
                row["raw_last_name"], row["raw_suffix"]) if x),
            "act_salutation": row["raw_salutation"],
            "act_company": row["raw_company"], "act_city": row["raw_city"],
            "act_state": row["raw_state"], "act_postal": row["raw_postal"],
            "act_email": row["norm_email"],
            # Both source records and evidence carry this field, so the first
            # merge suffixes it. Prefer the normalized evidence value and
            # retain fallbacks for older ledger schemas.
            "act_email_domain": report_email_domain(row),
            "expected_sec_firm_crds": row.get("email_domain_firms_json", ""),
            "actual_sec_firm_crds": row.get("current_firms_json", ""),
            "sec_name": sec_full,
            "sec_used_first": row["sec_used_first_name"],
            "candidate_tier": row["candidate_tier"],
            "candidate_score": row["candidate_score"],
            "candidate_gap": row["candidate_gap"],
            "positive_evidence": row["positive_evidence_json"],
            "hard_conflicts": row.get("hard_conflicts_json_link",
                                      row["hard_conflicts_json"]),
            "warnings": row.get("warnings_json_link", row["warnings_json"]),
            "roster_evidence": row["roster_evidence_json"],
            "expected_evidence_hash": row["evidence_hash"],
            "identity_manifest_hash": manifest["contentHash"],
            "act_source_file": (manifest.get("actSource") or {}).get("file", ""),
            "act_source_sha256": (manifest.get("actSource") or {}).get("sha256", ""),
            "link_decision": "", "resolved_crd": "",
            "canonical_for_crd": "", "act_record_action": "",
            "preferred_decision": "", "preferred_first": row["raw_salutation"],
            "reviewer": "", "reviewed_utc": "", "reason_code": "", "notes": "",
        }
    report = pd.DataFrame([report_row(r) for _, r in picked.iterrows()],
                          columns=REVIEW_COLUMNS)
    return report.sort_values(["priority", "act_full_name", "act_id"],
                              kind="stable").reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--candidate-limit", type=int, default=250)
    parser.add_argument("--include-act-id", action="append", default=[],
                        help="always include this Act GUID in the review report")
    args = parser.parse_args()
    report = build_review_frame(max(0, args.candidate_limit),
                                args.include_act_id)
    csv_path, xlsx_path = (IDENTITY / CORRECTIONS_CSV_FILENAME,
                           IDENTITY / CORRECTIONS_XLSX_FILENAME)
    report.to_csv(csv_path, index=False, encoding="utf-8-sig")
    instructions = pd.DataFrame({
        "Instructions": [
            "Edit only the decision columns at the right of the Review sheet.",
            "link_decision: approve, replace, or reject.",
            "resolved_crd: required for approve/replace; verify in IAPD.",
            "canonical_for_crd: TRUE only for the one Act record that owns a duplicated CRD.",
            "act_record_action: keep_crd, clear_crd, set_crd, merge_contact, fix_name, or review_only.",
            "preferred_decision is separate: approve or reject the greeting name.",
            "reviewer and reason_code are required for every submitted decision.",
            "Do not edit expected_evidence_hash. A changed Act/SEC record makes the decision stale and the importer rejects it.",
        ]})
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        instructions.to_excel(writer, sheet_name="Instructions", index=False)
        report.to_excel(writer, sheet_name="Review", index=False)
        sheet = writer.book["Review"]
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            font = copy.copy(cell.font)
            font.bold = True
            cell.font = font
        for column in sheet.columns:
            letter = column[0].column_letter
            width = min(48, max(12, max(len(str(c.value or "")) for c in column) + 2))
            sheet.column_dimensions[letter].width = width
    counts = report["issue_type"].value_counts().to_dict() if len(report) else {}
    print(f"[+] {csv_path}: {len(report):,} rows {counts}")
    print(f"[+] {xlsx_path}")


if __name__ == "__main__":
    main()
