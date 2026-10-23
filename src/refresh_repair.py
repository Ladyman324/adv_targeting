"""Bounded, private repair requests and review-only source proposals.

No model invocation, patch application, acquisition, or publication happens in
this module. A trusted runner may consume a queued request after authorization.
Source data, ACT exports, logs and credentials are never copied into requests.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re

import refresh_core as core

SOURCE = "roster:edward_jones"
ADAPTER = "src/edward_jones_async.py"
EDITABLE_FUNCTIONS = {"extract_emails", "extract_bio", "find_profile_url"}
PARSER_CODES = {"CSV_FORMAT_INVALID", "REQUIRED_PERSON_FIELD_MISSING", "FIELD_VALUES_LOST"}
KNOWN_CODES = PARSER_CODES | {"PERSON_IDS_INVALID", "INVALID_EMAIL_VALUES", "PROFILE_ACQUISITION_INCOMPLETE",
    "BASELINE_REVIEW_REQUIRED", "BASELINE_INVALID", "BASELINE_PERSON_IDS_INVALID", "PEOPLE_DISAPPEARED",
    "UNEXPECTED_PEOPLE_GROWTH", "FIELD_VALUES_CHANGED", "EMAIL_PERSON_ASSOCIATION_CHANGED",
    "NEW_SHARED_EMAIL_ASSOCIATIONS", "STATE_COVERAGE_LOST"}


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise core.RefreshError("Repair request requires a SHA-256 identity")
    return value


def queue_acquisition_failure(stage, source_id, code, baseline_hash, command_hash):
    """Record a command failure without treating transport as a parser defect.

    Only fixed categories and hashes enter this incident. In particular, never
    copy exceptions, command arguments, logs, URLs, ACT rows or credentials.
    """
    from refresh_acquire import _source
    _source(source_id)
    if code not in {"SOURCE_COMMAND_FAILED", "SOURCE_COMMAND_INTERRUPTED"}:
        raise core.RefreshError("Unsupported acquisition incident category")
    safe = {"sourceId": source_id, "code": code, "baselineSha256": _hash(baseline_hash),
            "commandSha256": _hash(command_hash)}
    identity = core.digest(safe)
    folder = core.contained(Path(stage), "data/refresh/acquisition_incidents/" + identity)
    with core.lock(folder):
        file = folder / "incident.json"
        if file.exists():
            value = core.read_json(file)
            if value.get("id") != identity or value.get("diagnostic") != safe:
                raise core.RefreshError("Acquisition incident identity mismatch")
            return value
        value = {"schemaVersion": 1, "id": identity, "diagnostic": safe,
                 "createdUtc": core.utc(), "status": "manual_review",
                 "modelAuthorized": False, "canApplyPatch": False, "canPublish": False}
        core.atomic_json(file, value)
        return value


def _safe_report(report):
    if (report.get("sourceId") != SOURCE or report.get("status") != "held"
            or report.get("policyVersion") != "edward-jones-history-v1"):
        raise core.RefreshError("Repair requests require a held pilot comparison")
    result = {"sourceId": SOURCE, "status": "held", "policyVersion": "edward-jones-history-v1",
              "candidateSha256": _hash(report.get("candidateSha256")),
              "baselineSha256": _hash(report["baselineSha256"]) if report.get("baselineSha256") else None,
              "issues": []}
    for item in report.get("issues", []):
        if not isinstance(item, dict) or item.get("code") not in KNOWN_CODES:
            raise core.RefreshError("Unrecognized repair diagnostic code")
        safe = {"code": item["code"]}
        for key in ("count", "missing", "duplicates", "before", "after"):
            if key in item and type(item[key]) is int and item[key] >= 0:
                safe[key] = item[key]
        if item.get("field") in {"name", "email", "phone", "city", "state", "address", "profile"}:
            safe["field"] = item["field"]
        result["issues"].append(safe)
    if not result["issues"]:
        raise core.RefreshError("Held repair diagnostic must identify an issue")
    return result


def queue_request(stage, report):
    """Idempotent incident creation; never overwrite a prior review decision."""
    stage = Path(stage)
    safe = _safe_report(report)
    adapter = stage / ADAPTER
    code_hash = core.file_hash(adapter) if adapter.is_file() and not adapter.is_symlink() else None
    identity = core.digest({"diagnostic": safe, "adapterSha256": code_hash})
    folder = stage / "data/refresh/repair_requests" / identity
    with core.lock(folder):
        file = folder / "request.json"
        if file.exists():
            current = core.read_json(file)
            if current.get("id") != identity or current.get("diagnostic") != safe or current.get("adapterSha256") != code_hash:
                raise core.RefreshError("Repair request identity mismatch")
            return current
        codes = {item["code"] for item in safe["issues"]}
        request = {"schemaVersion": 1, "id": identity, "createdUtc": core.utc(), "sourceId": SOURCE,
                   "diagnostic": safe, "adapterSha256": code_hash,
                   "status": "awaiting_diagnostic_review" if codes & PARSER_CODES and code_hash else "manual_review",
                   "modelAuthorized": False, "attempts": [], "maxAttempts": 1, "timeoutSeconds": 600,
                   "allowedFile": ADAPTER, "allowedFunctions": sorted(EDITABLE_FUNCTIONS),
                   "requires": ["approved_public_failure_fixture", "held_out_regression_tests",
                                "independent_review", "operator_patch_approval", "new_pinned_refresh_run"],
                   "canApplyPatch": False, "canPublish": False}
        core.atomic_json(file, request)
        return request


def _outside_parser_ast(text):
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in EDITABLE_FUNCTIONS:
            # Ignore only implementation bodies; decorators, arguments and
            # defaults remain protected against import-time side effects.
            node.body = [ast.Pass()]
    return ast.dump(tree, include_attributes=False)


def validate_proposal_source(original, replacement, summary):
    if not isinstance(replacement, str) or not 1 <= len(replacement.encode("utf-8")) <= 300000:
        raise core.RefreshError("Repair proposal source size is invalid")
    if not isinstance(summary, str) or not 12 <= len(summary) <= 2000:
        raise core.RefreshError("Repair proposal summary is invalid")
    try:
        before, after = _outside_parser_ast(original), _outside_parser_ast(replacement)
        changed = ast.dump(ast.parse(original)) != ast.dump(ast.parse(replacement))
    except (SyntaxError, ValueError) as exc:
        raise core.RefreshError("Repair proposal is not valid Python") from exc
    if before != after:
        raise core.RefreshError("Repair proposal changes protected code outside parser functions")
    if not changed:
        raise core.RefreshError("Repair proposal contains no semantic change")


def record_proposal(stage, request_id, replacement, summary):
    """Accept a *proposal artifact*, never modify the live or staged adapter.

    Even a structurally permitted patch remains unreviewed and unexecutable.
    Trusted validation and independent approval are separate required steps.
    """
    stage = Path(stage)
    _hash(request_id)
    folder = stage / "data/refresh/repair_requests" / request_id
    if not (folder / "request.json").is_file():
        raise core.RefreshError("Unknown repair request")
    with core.lock(folder):
        request = core.read_json(folder / "request.json")
        if request.get("status") not in {"awaiting_diagnostic_review", "awaiting_proposal"}:
            raise core.RefreshError("This incident requires manual review or already has a proposal")
        adapter = stage / ADAPTER
        if adapter.is_symlink() or core.file_hash(adapter) != request.get("adapterSha256"):
            raise core.RefreshError("Source changed; create a new repair incident")
        original = adapter.read_text(encoding="utf-8-sig")
        validate_proposal_source(original, replacement, summary)
        proposal = {"schemaVersion": 1, "requestId": request_id, "file": ADAPTER,
                    "originalSha256": request["adapterSha256"], "replacement": replacement,
                    "summary": summary, "status": "awaiting_validation_and_independent_review",
                    "canApplyPatch": False, "canPublish": False}
        file = folder / "proposal.json"
        if file.exists():
            raise core.RefreshError("Repair proposal is immutable; a second attempt is not permitted")
        core.atomic_json(file, proposal)
        request.update(status="proposal_awaiting_review", proposalHash=core.file_hash(file))
        core.atomic_json(folder / "request.json", request)
        return {key: value for key, value in proposal.items() if key != "replacement"}
