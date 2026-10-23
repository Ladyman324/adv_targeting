"""Operator decisions for exact held snapshots, separate from import/publication."""
from pathlib import Path
import re

import refresh_core as core

# Human review may accept genuine coverage/value changes, but cannot convert
# malformed, ambiguous-person-ID, or unfinished acquisition into valid data.
NON_OVERRIDABLE = {"CSV_FORMAT_INVALID", "PERSON_IDS_INVALID", "REQUIRED_PERSON_FIELD_MISSING",
                   "INVALID_EMAIL_VALUES", "PROFILE_ACQUISITION_INCOMPLETE",
                   "BASELINE_INVALID", "BASELINE_PERSON_IDS_INVALID", "SOURCE_ENTITY_IDENTITY_MISSING", "SOURCE_ID_AMBIGUITY",
                   "RANKING_FORMAT_INVALID", "SEC_FORMAT_INVALID", "SEC_REQUIRED_NAMES_MISSING"}


def _folder(stage, identity, source_id="roster:edward_jones"):
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise core.RefreshError("Comparison ID must be a SHA-256 identity")
    from refresh_history import source_key
    return core.contained(stage, "data/refresh/acquisition_reviews/" + source_key(source_id) + "/" + identity)


def accepted_decision(folder, report):
    file = Path(folder) / "decision.json"
    if not file.exists():
        return None
    if file.is_symlink():
        raise core.RefreshError("Historical review decision symlink refused")
    value = core.read_json(file)
    codes = sorted({item["code"] for item in report["issues"]})
    if (value.get("schemaVersion") != 1 or value.get("reportHash") != core.digest(report)
            or value.get("candidateSha256") != report["candidateSha256"]
            or value.get("baselineSha256") != report["baselineSha256"]
            or value.get("policyVersion") != report["policyVersion"]
            or value.get("acknowledgedIssues") != codes):
        raise core.RefreshError("Historical review decision no longer binds the assessed evidence")
    if value.get("decision") == "reject":
        return None
    if (value.get("decision") != "approve" or NON_OVERRIDABLE.intersection(codes)
            or not isinstance(value.get("reviewer"), str) or not value["reviewer"].strip()
            or not isinstance(value.get("reason"), str) or len(value["reason"].strip()) < 12):
        raise core.RefreshError("Historical review decision is invalid or tries to override unsafe input")
    return core.file_hash(file)


def review(root, run_id, identity, decision, reviewer, reason, acknowledged=(), source_id="roster:edward_jones"):
    """Write one immutable operator attestation; never select or publish data.

    The CLI is an operator-only local tool, not an API authorization mechanism.
    Reviewer identity is an explicit attestation, not an impersonation check.
    """
    from refresh_acquire import _compatible
    from refresh_resilience import compare_edward_jones, comparison_id
    if decision not in {"approve", "reject"}:
        raise core.RefreshError("Review decision must be approve or reject")
    if not isinstance(reviewer, str) or not 1 <= len(reviewer.strip()) <= 254:
        raise core.RefreshError("A named operator reviewer is required")
    if not isinstance(reason, str) or not 12 <= len(reason.strip()) <= 2000:
        raise core.RefreshError("Review reason must contain 12 to 2000 characters")
    directory, manifest, stage = core.load_run(root, run_id)
    _compatible(manifest, source_id)
    folder = _folder(stage, identity, source_id)
    with core.lock(directory):
        _, manifest, stage = core.load_run(root, run_id)
        _compatible(manifest, source_id)
        core.check_inputs(stage, manifest)
        from refresh_history import held_name
        report_file = folder / "comparison.json"
        if not report_file.is_file() or report_file.is_symlink():
            raise core.RefreshError("No regular held candidate and comparison exist for this review")
        report = core.read_json(report_file)
        held = folder / held_name(source_id, report.get("comparisonOptions"))
        if not held.is_file() or held.is_symlink():
            raise core.RefreshError("No regular held candidate and comparison exist for this review")
        if report.get("status") != "held" or comparison_id(report) != identity:
            raise core.RefreshError("Historical comparison identity mismatch")
        if report.get("sourceId") != source_id:
            raise core.RefreshError("Held comparison belongs to a different source")
        if source_id == "roster:edward_jones":
            prior = sorted((stage / "data/raw/firm_rosters").glob("edward_jones_*.csv"))[-1:]
            observed = compare_edward_jones(held, prior[0] if prior else None)
        else:
            from refresh_history import compare_source
            observed = compare_source(stage, source_id, held, report.get("comparisonOptions"))
        if observed != report:
            raise core.RefreshError("Held candidate or selected baseline changed; review a new comparison")
        codes = sorted({item["code"] for item in report["issues"]})
        if sorted(set(acknowledged)) != codes:
            raise core.RefreshError("Explicitly acknowledge every flagged issue code exactly")
        if decision == "approve" and NON_OVERRIDABLE.intersection(codes):
            raise core.RefreshError("Malformed, incomplete, or ambiguous-ID input cannot be approved")
        file = folder / "decision.json"
        if file.exists():
            raise core.RefreshError("Historical review decision is immutable; it cannot be overwritten")
        value = {"schemaVersion": 1, "comparisonId": identity, "reportHash": core.digest(report),
                 "policyVersion": report["policyVersion"], "candidateSha256": report["candidateSha256"],
                 "baselineSha256": report["baselineSha256"], "decision": decision,
                 "reviewer": reviewer.strip(), "reason": reason.strip(), "recordedUtc": core.utc(),
                 "acknowledgedIssues": codes, "selectsData": False, "authorizesPublication": False}
        core.atomic_json(file, value)
        manifest.setdefault("acquisitionReviews", []).append({"comparisonId": identity,
            "sourceId": source_id, "decision": decision, "decisionSha256": core.file_hash(file),
            "recordedUtc": value["recordedUtc"]})
        # Keep the prior failure visible. Only a successful new import/acquire
        # attempt may transition the source back to ready/success.
        core.save_run(directory, manifest)
        return {"comparisonId": identity, "decision": decision, "selectedDataChanged": False,
                "publicationAuthorized": False}
