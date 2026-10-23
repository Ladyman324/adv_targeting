"""Explicit, single-use retry attestations. Never invokes a model."""
from pathlib import Path

import refresh_core as core
import refresh_repair as repair

MAX_TOTAL_ATTEMPTS = 3


def check(folder, request):
    attempts = request.get("attempts", [])
    number = len(attempts) + 1
    file = folder / ("retry-authorization-" + str(number) + ".json")
    if (request.get("status") != "retry_authorized" or not 1 <= len(attempts) < MAX_TOTAL_ATTEMPTS
            or file.is_symlink() or not file.is_file()
            or core.file_hash(file) != request.get("retryAuthorizationHash")):
        raise core.RefreshError("No unchanged single-use retry authorization exists")
    value = core.read_json(file)
    if (value.get("requestId") != request.get("id") or value.get("attemptNumber") != number
            or value.get("previousAttemptsHash") != core.digest(attempts)
            or value.get("adapterSha256") != request.get("adapterSha256")
            or value.get("acknowledgesPossiblePriorProviderCharge") is not True):
        raise core.RefreshError("Retry authorization does not bind the failed attempt history")
    return value


def authorize(stage, request_id, *, operator, reason, acknowledge_possible_charge=False):
    if not isinstance(operator, str) or not 1 <= len(operator.strip()) <= 254:
        raise core.RefreshError("A named retry authorizer is required")
    if not isinstance(reason, str) or not 12 <= len(reason.strip()) <= 2000:
        raise core.RefreshError("A 12 to 2000 character retry reason is required")
    if acknowledge_possible_charge is not True:
        raise core.RefreshError("Acknowledge that an uncertain prior call may still incur a charge")
    stage = Path(stage); repair._hash(request_id)
    folder = core.contained(stage, "data/refresh/repair_requests/" + request_id)
    with core.lock(folder):
        file = folder / "request.json"
        if file.is_symlink() or not file.is_file():
            raise core.RefreshError("Unknown regular repair request")
        request = core.read_json(file)
        attempts = request.get("attempts", [])
        if (request.get("status") != "proposal_failed" or not 1 <= len(attempts) < MAX_TOTAL_ATTEMPTS
                or any(a.get("status") not in {"failed", "incomplete"} or not a.get("finishedUtc") for a in attempts)
                or any((folder / name).exists() for name in ("proposal.json", "model-proposal.json"))):
            raise core.RefreshError("Only terminal failed proposals without artifacts can be reauthorized; maximum three attempts")
        safe = repair._safe_report(request["diagnostic"])
        if request.get("id") != request_id or core.digest({"diagnostic": safe,
                "adapterSha256": request.get("adapterSha256")}) != request_id:
            raise core.RefreshError("Repair incident identity mismatch")
        adapter = stage / repair.ADAPTER
        if adapter.is_symlink() or core.file_hash(adapter) != request.get("adapterSha256"):
            raise core.RefreshError("Pinned adapter changed; create a new incident")
        value = {"schemaVersion": 1, "requestId": request_id, "attemptNumber": len(attempts) + 1,
                 "previousAttemptsHash": core.digest(attempts), "adapterSha256": request["adapterSha256"],
                 "operator": operator.strip(), "reason": reason.strip(), "recordedUtc": core.utc(),
                 "acknowledgesPossiblePriorProviderCharge": True, "modelInvoked": False}
        decision = folder / ("retry-authorization-" + str(value["attemptNumber"]) + ".json")
        if decision.exists():
            raise core.RefreshError("Retry authorization is immutable; inspect the existing decision")
        core.atomic_json(decision, value)
        request.update(status="retry_authorized", retryAuthorizationHash=core.file_hash(decision),
                       maxAttempts=value["attemptNumber"])
        core.atomic_json(file, request)
        return value


def for_run(root, run_id, request_id, **options):
    from refresh_acquire import _compatible
    directory, manifest, stage = core.load_run(root, run_id)
    _compatible(manifest, repair.SOURCE)
    with core.lock(directory):
        _, manifest, stage = core.load_run(root, run_id)
        _compatible(manifest, repair.SOURCE)
        core.check_inputs(stage, manifest)
        result = authorize(stage, request_id, **options)
        file = stage / "data/refresh/repair_requests" / request_id / "request.json"
        manifest.setdefault("repairRequests", {})[request_id] = {
            "status": core.read_json(file)["status"], "requestSha256": core.file_hash(file)}
        core.save_run(directory, manifest)
        return result
