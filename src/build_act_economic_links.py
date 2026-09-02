"""Build a hash-bound, ACT-only asset-to-SEC economic link ledger."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from act_book import build as build_book, slots
from act_economic_links import (LINK_COLUMNS, RULESET_VERSION, SCHEMA_VERSION,
                                account_bucket, approved_link_map,
                                choose_with_margin,
                                enforce_global_one_to_one, firm_agreement,
                                json_list, paired_location, personal_email,
                                safety_fields, sec_name_agreement,
                                spreadsheet_safe,
                                unique_validated_roster_email)
from build_contacts import load_index, load_rosters, score_contacts
from build_identity_ledger import sec_context
from contact_provenance import sha256_file
from export_act_crd_corrections import validate_identity_manifest
from identity_normalize import clean_text, name_token, normalize_crd
from identity_schema import content_hash
from roster_firm_policy import build_domain_policy, crd_tuple

IDENTITY = ROOT / "data" / "identity"
RAW = ROOT / "data" / "raw"
INTERIM = ROOT / "data" / "interim"
LEDGER = IDENTITY / "act_economic_links.parquet"
MANIFEST = IDENTITY / "act_economic_manifest.json"
REVIEW_CSV = IDENTITY / "act_economic_review.csv"
REVIEW_XLSX = IDENTITY / "act_economic_review.xlsx"


def atomic_json(path: pathlib.Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def atomic_parquet(frame: pd.DataFrame, path: pathlib.Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    frame.to_parquet(tmp, index=False)
    tmp.replace(path)


def exact_act_snapshot(manifest: dict) -> tuple[pathlib.Path, list[dict]]:
    fact = manifest.get("actSource") or {}
    path = RAW / pathlib.Path(clean_text(fact.get("file"))).name
    if not path.is_file() or sha256_file(path) != fact.get("sha256"):
        raise SystemExit("ACT JSON differs from identity manifest; rebuild identity first")
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or len(rows) != int(fact.get("rows") or -1):
        raise SystemExit("ACT JSON row count differs from identity manifest")
    return path, rows


def validate_pinned_inputs(manifest: dict) -> None:
    """Refuse live SEC/roster inputs that differ from the identity build."""
    expected_sec = manifest.get("secSources") or {}
    required_sec = {
        "advisors.parquet", "advisor_branches.parquet",
        "advisor_employments.parquet", "advisor_employment_history.parquet",
        "advisor_other_names.parquet",
    }
    if set(expected_sec) != required_sec:
        raise SystemExit("identity manifest has an incomplete SEC source set")
    for name, expected_hash in expected_sec.items():
        path = INTERIM / pathlib.Path(name).name
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise SystemExit(f"SEC source differs from identity manifest: {name}")

    if manifest.get("rosterEvidence") is not True:
        raise SystemExit("identity manifest did not include roster evidence")
    expected_rosters = manifest.get("rosterSources") or {}
    roster_dir = RAW / "firm_rosters"
    actual_rosters = {
        path.name: sha256_file(path)
        for path in sorted(roster_dir.glob("*")) if path.is_file()}
    if actual_rosters != expected_rosters:
        missing = sorted(set(expected_rosters) - set(actual_rosters))
        added = sorted(set(actual_rosters) - set(expected_rosters))
        changed = sorted(
            name for name in set(actual_rosters) & set(expected_rosters)
            if actual_rosters[name] != expected_rosters[name])
        raise SystemExit(
            "firm roster inputs differ from identity manifest "
            f"(missing={missing}, added={added}, changed={changed})")


def validate_domain_policy(manifest: dict, domain_policy: dict) -> str:
    """Bind the derived roster-domain policy to the identity build."""
    actual_hash = content_hash(domain_policy)
    expected_hash = clean_text(manifest.get("rosterDomainPolicyHash"))
    if not expected_hash or actual_hash != expected_hash:
        raise SystemExit("roster domain policy differs from identity manifest")
    return actual_hash


def asset_holders(rows: list[dict]):
    holders = collections.defaultdict(set)
    for row in rows:
        for code, *_ in slots(row.get("customFields") or {}):
            if code:
                holders[str(row.get("id") or "")].add(code)
    accounts, _conflicts, unresolved = build_book(rows)
    return dict(holders), accounts, unresolved


def validate_act_ids(rows: list[dict]) -> None:
    """Fail before any dictionary/set collapse can hide a bad ACT GUID."""
    seen = set()
    for position, row in enumerate(rows, start=1):
        act_id = clean_text(row.get("id"))
        if not act_id:
            raise SystemExit(f"ACT snapshot has a blank id at row {position}")
        if act_id in seen:
            raise SystemExit(f"ACT snapshot has a duplicate id: {act_id}")
        seen.add(act_id)


def candidate_score(name_reason: str, location_strength: int,
                    firm_reason: str) -> float:
    name = {"given_exact": .60, "given_sec_used": .58,
            "given_strict_nickname": .54}.get(name_reason, .50)
    firm = .25 if firm_reason == "approved_firm_family" else .22
    location = .13 + .04 * min(2, max(0, location_strength - 1))
    return round(name + firm + location, 3)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--review-limit", type=int, default=250)
    args = ap.parse_args(argv)
    identity_manifest = validate_identity_manifest()
    validate_pinned_inputs(identity_manifest)
    act_path, act_rows = exact_act_snapshot(identity_manifest)
    validate_act_ids(act_rows)
    holders, accounts, unresolved_accounts = asset_holders(act_rows)
    if unresolved_accounts:
        raise SystemExit(
            "ACT account-value conflicts remain unresolved; refusing economic build")

    source = pd.read_parquet(IDENTITY / "act_source_records.parquet").fillna("")
    evidence = pd.read_parquet(IDENTITY / "act_identity_evidence.parquet").fillna("")
    identity = pd.read_parquet(IDENTITY / "act_identity_links.parquet").fillna("")
    joined = source.merge(
        evidence, on=["record_key", "source_record_id"],
        suffixes=("_act", "_ev"), validate="one_to_one").merge(
        identity[["record_key", "source_record_id", "advisor_crd",
                  "identity_status", "resolved_evidence_hash"]],
        on=["record_key", "source_record_id"], suffixes=("", "_identity"),
        validate="one_to_one")
    joined = joined[joined["source_record_id"].astype(str).isin(holders)].copy()
    records = {str(r["source_record_id"]): r for r in joined.to_dict("records")}
    if set(records) != set(holders):
        raise SystemExit("ACT asset holders do not reconcile to identity source rows")
    sec, current, _prior, branches, aliases = sec_context()

    # Reuse the production roster matcher unchanged. Only its already validated
    # confirmed/high rows may donate a CRD to the exact-email pass.
    roster_raw = load_rosters().fillna("")
    roster_scored = score_contacts(roster_raw, load_index()).fillna("")
    validated_roster = unique_validated_roster_email(
        roster_scored.to_dict("records"))
    domain_policy = build_domain_policy(roster_raw.to_dict("records"))
    domain_policy_hash = validate_domain_policy(identity_manifest, domain_policy)

    results = {}
    claimed = set()
    for act_id, rec in records.items():
        crd = normalize_crd(rec.get("advisor_crd_identity"))
        if rec.get("identity_status") == "approved" and crd:
            results[act_id] = (crd, "approved", "identity_approved",
                               "strict_identity_approved", 1.0, 1.0,
                               {"identityStatus": "approved"})
            claimed.add(crd)

    # Pass 1: exact personal email, unique in ACT and in validated roster rows.
    roster_proposals = {}
    roster_meta = {}
    for act_id, rec in records.items():
        if act_id in results or int(rec.get("email_claim_count") or 0) != 1:
            continue
        roster = validated_roster.get(personal_email(rec.get("norm_email")))
        if not roster:
            continue
        crd = normalize_crd(roster.get("advisor_crd"))
        allowed = crd_tuple(roster.get("allowed_firm_crds"))
        name_ok, name_reason = sec_name_agreement(
            rec, sec.get(crd, {}), aliases.get(crd, []))
        firm_ok, firm_reason = firm_agreement(
            rec, current.get(crd, {}), allowed)
        current_crds = set(current.get(crd, {}).get("firm_crds", set()))
        roster_meta[act_id] = {
            "name": name_reason, "firm": firm_reason,
            "rosterSource": clean_text(roster.get("source_file"))}
        if (crd and crd not in claimed and name_ok and firm_ok and allowed
                and current_crds & set(allowed)):
            roster_proposals[act_id] = (crd, 1.0, 1.0, "winner")
        else:
            reason = ("roster_exact_name_conflict" if not name_ok else
                      "roster_exact_firm_conflict")
            results[act_id] = (crd, "review", "", reason, 1.0, 0.0,
                               roster_meta[act_id])
    roster_proposals = enforce_global_one_to_one(roster_proposals, claimed)
    for act_id, (crd, score, gap, verdict) in roster_proposals.items():
        if verdict == "winner":
            results[act_id] = (
                crd, "approved", "roster_exact",
                "unique_personal_roster_email", score, gap, roster_meta[act_id])
            claimed.add(crd)
        else:
            results[act_id] = (
                crd, "review", "", verdict, score, gap, roster_meta[act_id])

    # Global residual pass over unclaimed CRDs. Every candidate must pass name,
    # paired city/state, and current-firm gates; no later relaxed pass exists.
    surname_index = collections.defaultdict(set)
    for crd, person in sec.items():
        surname_index[name_token(person.get("last_name"))].add(crd)
    for crd, names in aliases.items():
        for person in names:
            surname_index[name_token(person.get("last_name"))].add(crd)
    residual = {}
    residual_meta = {}
    for act_id, rec in records.items():
        if act_id in results:
            continue
        allowed = set()
        policy = domain_policy.get(clean_text(rec.get("email_domain_ev")), {})
        if policy.get("status") == "authoritative":
            allowed.update(crd_tuple(policy.get("allowedFirmCrds")))
        scored, metadata = [], {}
        for crd in surname_index.get(name_token(rec.get("norm_last_name")), set()):
            if crd in claimed:
                continue
            name_ok, name_reason = sec_name_agreement(
                rec, sec.get(crd, {}), aliases.get(crd, []),
                require_suffix_presence_match=True)
            loc_ok, loc_strength = paired_location(rec, branches.get(crd, []))
            firm_ok, firm_reason = firm_agreement(
                rec, current.get(crd, {}), allowed)
            if not (name_ok and loc_ok and firm_ok):
                continue
            score = candidate_score(name_reason, loc_strength, firm_reason)
            scored.append((score, crd))
            metadata[crd] = {
                "name": name_reason, "locationStrength": loc_strength,
                "firm": firm_reason}
        proposal = choose_with_margin(scored)
        residual[act_id] = proposal
        residual_meta[act_id] = metadata.get(proposal[0], {})
    residual = enforce_global_one_to_one(residual, claimed)
    for act_id, (crd, score, gap, verdict) in residual.items():
        if verdict == "winner":
            results[act_id] = (
                crd, "approved", "residual_strict",
                "unique_name_location_firm_residual", score, gap,
                residual_meta[act_id])
            claimed.add(crd)
        else:
            legacy = normalize_crd(records[act_id].get("candidate_crd"))
            reason = verdict if verdict != "no_strict_candidate" else (
                "no_strict_candidate" if not legacy else
                "legacy_candidate_failed_strict_gates")
            results[act_id] = (
                normalize_crd(crd or legacy),
                "review" if crd or legacy else "unmatched", "",
                reason, score, gap, residual_meta[act_id])

    ledger_rows = []
    for act_id, rec in records.items():
        crd, status, link_type, reason, score, gap, facts = results[act_id]
        codes = holders[act_id]
        current_crds = current.get(crd, {}).get("firm_crds", set()) if crd else set()
        policy = domain_policy.get(clean_text(rec.get("email_domain_ev")), {})
        allowed = (crd_tuple(policy.get("allowedFirmCrds"))
                   if policy.get("status") == "authoritative" else ())
        ledger_rows.append({
            "act_id": act_id,
            "advisor_crd": crd if status == "approved" else "",
            "economic_status": status, "link_type": link_type, "reason": reason,
            "candidate_crd": crd, "candidate_score": float(score),
            "candidate_gap": float(gap), "account_codes_json": json_list(codes),
            "account_count": len(codes),
            "relationship_value": round(sum(accounts[x]["value"] for x in codes), 2),
            "act_name": clean_text(rec.get("raw_full_name")),
            "act_company": clean_text(rec.get("raw_company")),
            "act_city": clean_text(rec.get("raw_city")),
            "act_state": clean_text(rec.get("raw_state")).upper(),
            "act_email": clean_text(rec.get("norm_email")),
            "current_firm_crds_json": json_list(current_crds),
            "allowed_firm_crds_json": json_list(allowed),
            "evidence_json": json.dumps(
                facts, sort_keys=True, separators=(",", ":")),
            "identity_evidence_hash": clean_text(
                rec.get("resolved_evidence_hash")),
            "ruleset_version": RULESET_VERSION, **safety_fields(),
        })
    ledger = pd.DataFrame(ledger_rows, columns=LINK_COLUMNS).sort_values("act_id")
    try:
        approved_link_map(ledger.to_dict("records"))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    IDENTITY.mkdir(parents=True, exist_ok=True)
    atomic_parquet(ledger, LEDGER)

    review = ledger[ledger["economic_status"] != "approved"].copy().sort_values(
        ["relationship_value", "act_name"], ascending=[False, True])
    review.insert(0, "priority", range(1, len(review) + 1))
    review_columns = [
        "priority", "act_id", "candidate_crd", "act_name", "act_company",
        "act_city", "act_state", "act_email", "relationship_value",
        "account_count", "reason", "candidate_score", "candidate_gap",
        "evidence_json", "identity_evidence_hash",
    ]
    review = review[review_columns].applymap(spreadsheet_safe)
    review_csv_tmp = REVIEW_CSV.with_suffix(REVIEW_CSV.suffix + ".tmp")
    review.to_csv(review_csv_tmp, index=False, encoding="utf-8-sig")
    review_csv_tmp.replace(REVIEW_CSV)
    workbook_review = review.head(max(0, args.review_limit))
    review_xlsx_tmp = REVIEW_XLSX.with_name(
        REVIEW_XLSX.stem + ".tmp" + REVIEW_XLSX.suffix)
    with pd.ExcelWriter(review_xlsx_tmp, engine="openpyxl") as writer:
        pd.DataFrame({"Instructions": [
            "Research-only report; no automated importer consumes this file.",
            "Economic links display ACT assets only; never email, call, rename, sync, or write CRDs to ACT.",
            "Review highest-dollar rows first and record any resolution through the approved identity-decision workflow.",
        ]}).to_excel(writer, sheet_name="Instructions", index=False)
        workbook_review.to_excel(writer, sheet_name="Review", index=False)
        sheet = writer.book["Review"]
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
    review_xlsx_tmp.replace(REVIEW_XLSX)

    by_account = collections.defaultdict(list)
    row_by_id = {row["act_id"]: row for row in ledger_rows}
    for act_id, codes in holders.items():
        for code in codes:
            by_account[code].append(row_by_id[act_id])
    reconciliation = collections.defaultdict(lambda: {
        "accounts": 0, "acv": 0.0, "lcv": 0.0, "mf": 0.0, "midcap": 0.0})
    for code, account in accounts.items():
        rows_for_account = by_account.get(code, [])
        bucket = account_bucket(
            [x["link_type"] for x in rows_for_account
             if x["economic_status"] == "approved"],
            [x["economic_status"] for x in rows_for_account])
        out = reconciliation[bucket]
        out["accounts"] += 1
        for key, source_key in (("acv", "acv_sma"), ("lcv", "large"),
                                ("mf", "fund"), ("midcap", "midcap")):
            out[key] += account[source_key]
    reconciliation = {
        key: {name: value if name == "accounts" else round(value, 2)
              for name, value in values.items()}
        for key, values in sorted(reconciliation.items())}
    core = {
        "schemaVersion": SCHEMA_VERSION, "rulesetVersion": RULESET_VERSION,
        "identityManifestHash": identity_manifest["contentHash"],
        "rosterDomainPolicyHash": domain_policy_hash,
        "actSource": {"file": act_path.name, "sha256": sha256_file(act_path),
                      "rows": len(act_rows)},
        "secSources": identity_manifest.get("secSources", {}),
        "rosterSources": identity_manifest.get("rosterSources", {}),
        "ledger": {"file": LEDGER.name, "sha256": sha256_file(LEDGER),
                   "rows": len(ledger)},
        "review": {"csv": REVIEW_CSV.name,
                   "csvSha256": sha256_file(REVIEW_CSV),
                   "xlsx": REVIEW_XLSX.name,
                   "xlsxSha256": sha256_file(REVIEW_XLSX),
                   "totalRows": len(review),
                   "totalValue": round(float(review["relationship_value"].sum()), 2),
                   "exportedRows": len(workbook_review),
                   "exportedValue": round(float(
                       workbook_review["relationship_value"].sum()), 2),
                   "omittedRows": len(review) - len(workbook_review),
                   "omittedValue": round(float(
                       review["relationship_value"].sum() -
                       workbook_review["relationship_value"].sum()), 2)},
        "statusCounts": ledger["economic_status"].value_counts().to_dict(),
        "linkTypeCounts": ledger["link_type"].replace(
            "", "unresolved").value_counts().to_dict(),
        "accounts": len(accounts),
        "unresolvedAccountValueConflicts": len(unresolved_accounts),
        "reconciliation": reconciliation,
    }
    economic_manifest = {
        **core, "generatedUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "contentHash": content_hash(core)}
    atomic_json(MANIFEST, economic_manifest)
    print(f"[+] {LEDGER}: {len(ledger):,} ACT asset holders")
    print(f"[+] {REVIEW_CSV}: {len(review):,} unresolved rows; "
          f"workbook contains top {len(workbook_review):,}")
    print(f"[+] reconciliation: {reconciliation}")


if __name__ == "__main__":
    main()
