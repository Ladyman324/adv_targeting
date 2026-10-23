"""Independent local operator attestation and export of tested parser proposals.

These commands never apply a source change or publish/accept contact data.
Operator identities are named attestations, not public API authentication.
"""
from __future__ import annotations

import difflib
import hashlib
from pathlib import Path
import re

import refresh_core as core
import refresh_repair as repair
from refresh_repair_runner import _fixture


def _folder(stage, identity):
    repair._hash(identity)
    return core.contained(stage, "data/refresh/repair_requests/" + identity)


def _proposal(stage, request_id):
    folder = _folder(stage, request_id)
    request = core.read_json(folder / "request.json")
    proposal_file = folder / "proposal.json"
    if proposal_file.is_symlink() or core.file_hash(proposal_file) != request.get("proposalHash"):
        raise core.RefreshError("Repair proposal no longer matches the incident")
    proposal = core.read_json(proposal_file)
    adapter = Path(stage) / repair.ADAPTER
    if adapter.is_symlink() or core.file_hash(adapter) != request.get("adapterSha256"):
        raise core.RefreshError("Pinned parser source changed before decision/export")
    original = adapter.read_text(encoding="utf-8-sig")
    repair.validate_proposal_source(original, proposal["replacement"], proposal["summary"])
    return folder, request, proposal, original


def _passed_evidence(folder, request):
    pointer = request.get("validationEvidence") or {}
    name = pointer.get("file", "")
    if not isinstance(name, str) or not re.fullmatch(r"validation-[1-9][0-9]*\.json", name):
        raise core.RefreshError("No trusted parser validation evidence exists")
    file = folder / name
    if file.is_symlink() or core.file_hash(file) != pointer.get("sha256"):
        raise core.RefreshError("Trusted parser validation evidence changed")
    evidence = core.read_json(file)
    if (evidence != request.get("validation") or evidence.get("status") != "passed"
            or evidence.get("proposalHash") != request.get("proposalHash")):
        raise core.RefreshError("Parser proposal has no matching passed validation")
    result = evidence.get("result") or {}
    if not all(result.get(key) is True for key in ("failureReproduced", "failureFixed", "regressionsPassed")):
        raise core.RefreshError("Parser proposal did not reproduce, fix and preserve regressions")
    worker = Path(__file__).with_name("refresh_repair_worker.py")
    controller = Path(__file__).with_name("refresh_repair_validation.py")
    limiter = Path(__file__).with_name("refresh_process_limits.py")
    if (core.file_hash(worker) != evidence.get("workerSha256")
            or core.file_hash(controller) != evidence.get("controllerSha256")
            or core.file_hash(limiter) != evidence.get("resourceLimiterSha256")):
        raise core.RefreshError("Trusted parser validation implementation changed; validate again")
    failure = _fixture(folder / "approved-fixture.json")
    if (core.digest(failure) != evidence.get("failureFixtureHash")
            or core.digest(failure) != request.get("authorization", {}).get("fixtureHash")):
        raise core.RefreshError("Authorized failure fixture changed")
    regression_name = evidence.get("regressionFixtureFile", "")
    if not isinstance(regression_name, str) or not re.fullmatch(r"regression-[1-9][0-9]*\.json", regression_name):
        raise core.RefreshError("No valid held-out regression fixture is bound")
    if core.digest(_fixture(folder / regression_name)) != evidence.get("regressionFixtureHash"):
        raise core.RefreshError("Held-out regression fixture changed")
    return pointer["sha256"]


def decide(stage, request_id, *, decision, reviewer, reason):
    if decision not in {"approve", "reject"}:
        raise core.RefreshError("Repair decision must be approve or reject")
    if not isinstance(reviewer, str) or not 1 <= len(reviewer.strip()) <= 254:
        raise core.RefreshError("A named independent reviewer is required")
    if not isinstance(reason, str) or not 12 <= len(reason.strip()) <= 2000:
        raise core.RefreshError("Repair review reason must contain 12 to 2000 characters")
    folder = _folder(stage, request_id)
    with core.lock(folder):
        folder, request, proposal, original = _proposal(stage, request_id)
        file = folder / "repair-decision.json"
        if file.exists():
            raise core.RefreshError("Repair decision is immutable")
        submitter = request.get("authorization", {}).get("operator")
        if not isinstance(submitter, str) or not submitter.strip():
            raise core.RefreshError("Repair proposal requires an identified submitter")
        if submitter.strip().casefold() == reviewer.strip().casefold():
            raise core.RefreshError("The proposal authorizer cannot approve their own repair")
        validation_hash = _passed_evidence(folder, request) if decision == "approve" else None
        value = {"schemaVersion": 1, "requestId": request_id, "decision": decision,
                 "reviewer": reviewer.strip(), "submitter": submitter.strip(), "reason": reason.strip(),
                 "recordedUtc": core.utc(), "proposalHash": request["proposalHash"],
                 "originalSha256": request["adapterSha256"], "validationHash": validation_hash,
                 "authorizesExportOnly": decision == "approve", "requiresNewRefreshRun": True,
                 "canApplyPatch": False, "canPublish": False}
        core.atomic_json(file, value)
        request.update(status="repair_approved" if decision == "approve" else "repair_rejected",
                       decisionHash=core.file_hash(file))
        core.atomic_json(folder / "request.json", request)
        return value


def export(stage, request_id):
    folder = _folder(stage, request_id)
    with core.lock(folder):
        folder, request, proposal, original = _proposal(stage, request_id)
        decision_file = folder / "repair-decision.json"
        if (request.get("status") != "repair_approved" or decision_file.is_symlink()
                or core.file_hash(decision_file) != request.get("decisionHash")):
            raise core.RefreshError("No unchanged independent approval authorizes repair export")
        decision = core.read_json(decision_file)
        validation_hash = _passed_evidence(folder, request)
        if (decision.get("decision") != "approve" or decision.get("proposalHash") != request.get("proposalHash")
                or decision.get("validationHash") != validation_hash
                or decision.get("originalSha256") != request.get("adapterSha256")):
            raise core.RefreshError("Repair decision no longer binds validated source and proposal")
        patch = "".join(difflib.unified_diff(original.splitlines(keepends=True),
            proposal["replacement"].splitlines(keepends=True), fromfile="a/" + repair.ADAPTER,
            tofile="b/" + repair.ADAPTER))
        result = {"schemaVersion": 1, "requestId": request_id, "file": repair.ADAPTER,
                  "originalSha256": request["adapterSha256"], "proposalHash": request["proposalHash"],
                  "decisionHash": request["decisionHash"], "validationHash": validation_hash,
                  "replacementSha256": hashlib.sha256(proposal["replacement"].encode("utf-8")).hexdigest(),
                  "unifiedDiff": patch, "replacement": proposal["replacement"],
                  "requiresNewRefreshRun": True, "canApplyPatch": False, "canPublish": False}
        file = folder / "approved-change.json"
        if file.exists() and core.read_json(file) != result:
            raise core.RefreshError("Immutable approved repair export differs")
        if not file.exists():
            core.atomic_json(file, result)
        return {"requestId": request_id, "exportFile": file.name, "exportSha256": core.file_hash(file),
                "requiresNewRefreshRun": True, "sourceModified": False, "publicationAuthorized": False}


def for_run(root, run_id, request_id, *, action, **options):
    from refresh_acquire import _compatible
    directory, manifest, stage = core.load_run(root, run_id)
    _compatible(manifest, repair.SOURCE)
    with core.lock(directory):
        _, manifest, stage = core.load_run(root, run_id)
        _compatible(manifest, repair.SOURCE)
        core.check_inputs(stage, manifest)
        if action == "review":
            result = decide(stage, request_id, **options)
        elif action == "export":
            result = export(stage, request_id)
        else:
            raise core.RefreshError("Unknown repair decision action")
        file = _folder(stage, request_id) / "request.json"
        manifest.setdefault("repairRequests", {})[request_id] = {
            "status": core.read_json(file).get("status"), "requestSha256": core.file_hash(file)}
        core.save_run(directory, manifest)
        return result
