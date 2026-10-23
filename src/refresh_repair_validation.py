"""Trusted, bounded fixture validation separate from proposal generation."""
from __future__ import annotations

import json
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import refresh_core as core
import refresh_repair as repair
from refresh_repair_runner import _fixture
from refresh_process_limits import attach, MEMORY_BYTES


def _stop(process):
    if process is not None and process.poll() is None:
        process.kill()
        process.communicate()


def validate(stage, request_id, regression_fixture, *, timeout_seconds=10):
    stage = Path(stage); repair._hash(request_id)
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 45:
        raise core.RefreshError("Parser validation wall time must be 1 to 45 seconds")
    folder = core.contained(stage, "data/refresh/repair_requests/" + request_id)
    if not (folder / "request.json").is_file():
        raise core.RefreshError("Unknown repair request")
    regression = _fixture(regression_fixture)
    with core.lock(folder):
        request = core.read_json(folder / "request.json")
        if request.get("status") != "proposal_awaiting_review":
            raise core.RefreshError("Repair proposal is not ready for trusted validation")
        proposal_file = folder / "proposal.json"
        if (proposal_file.is_symlink() or not proposal_file.is_file()
                or core.file_hash(proposal_file) != request.get("proposalHash")):
            raise core.RefreshError("Repair proposal hash does not match incident")
        proposal_hash = core.file_hash(proposal_file)
        proposal = core.read_json(proposal_file)
        adapter = stage / repair.ADAPTER
        if adapter.is_symlink() or core.file_hash(adapter) != request.get("adapterSha256"):
            raise core.RefreshError("Pinned parser source changed")
        original = adapter.read_text(encoding="utf-8-sig")
        repair.validate_proposal_source(original, proposal["replacement"], proposal["summary"])
        failure_file = folder / "approved-fixture.json"
        failure = _fixture(failure_file)
        if core.digest(failure) != request.get("authorization", {}).get("fixtureHash"):
            raise core.RefreshError("Failure fixture no longer binds proposal authorization")
        if core.digest(failure) == core.digest(regression):
            raise core.RefreshError("Held-out regression fixture must differ from the failure fixture")
        if {core.digest(case) for case in failure["cases"]} & {core.digest(case) for case in regression["cases"]}:
            raise core.RefreshError("Held-out regression cases must not overlap model-visible cases")
        worker = Path(__file__).with_name("refresh_repair_worker.py").resolve()
        worker_hash = core.file_hash(worker)
        payload = {"original": original, "replacement": proposal["replacement"],
                   "failureFixture": failure, "regressionFixture": regression}
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > 500000:
            raise core.RefreshError("Parser validation payload exceeds limit")
        logs = folder / "logs"; logs.mkdir(exist_ok=True)
        existing = list(logs.glob("validation-*.process.json"))
        number = max([int(file.name.split("-")[1].split(".")[0]) for file in existing] or [0]) + 1
        receipt_file = logs / ("validation-" + str(number) + ".process.json")
        regression_name = "regression-" + str(number) + ".json"
        core.atomic_json(folder / regression_name, regression)
        attempt = {"status": "running", "startedUtc": core.utc(), "timeoutSeconds": timeout_seconds,
                   "proposalHash": proposal_hash, "workerSha256": worker_hash,
                   "controllerSha256": core.file_hash(Path(__file__)),
                   "resourceLimiterSha256": core.file_hash(Path(__file__).with_name("refresh_process_limits.py")),
                   "memoryLimitBytes": MEMORY_BYTES,
                   "failureFixtureHash": core.digest(failure), "regressionFixtureHash": core.digest(regression),
                   "regressionFixtureFile": regression_name}
        request["validation"] = attempt
        core.atomic_json(folder / "request.json", request)
        process = None
        close_limits = None
        receipt = {"status": "starting", "startedUtc": attempt["startedUtc"], "log": receipt_file.name}
        try:
            # Do not inherit tokens, connection strings, browser sessions,
            # Python startup/module path hooks, or the user's HOME/CODEX_HOME.
            environment = {key: value for key, value in os.environ.items()
                           if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
            with ExitStack() as cleanup:
                scratch = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="parser-validation-"))
                # On Windows a live child's cwd prevents temporary-directory
                # cleanup. Reap before leaving that context on *every* error.
                cleanup.callback(lambda: _stop(process))
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                process = subprocess.Popen([sys.executable, "-I", "-B", str(worker)], cwd=scratch,
                    env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=flags)
                identity = core.process_identity(process.pid)
                receipt = {"status": "running", "pid": process.pid, "created": identity["created"],
                           "startedUtc": attempt["startedUtc"], "log": receipt_file.name}
                core.atomic_json(receipt_file, receipt)
                close_limits = attach(process)
                try:
                    output, errors = process.communicate(input=encoded, timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    process.kill(); process.communicate()
                    raise core.RefreshError("Restricted parser validation exceeded its wall-time limit") from None
                if len(output) > 100000 or len(errors) > 100000:
                    raise core.RefreshError("Restricted parser worker exceeded output limit")
                try:
                    result = json.loads(output)
                except (ValueError, UnicodeError):
                    raise core.RefreshError("Restricted parser worker did not return valid evidence") from None
                if not isinstance(result, dict) or result.get("status") not in {"passed", "failed"}:
                    raise core.RefreshError("Restricted parser worker returned invalid status")
                if result["status"] == "passed" and process.returncode != 0:
                    raise core.RefreshError("Parser worker exit disagrees with its evidence")
                if core.file_hash(adapter) != request["adapterSha256"] or core.file_hash(proposal_file) != proposal_hash:
                    raise core.RefreshError("Parser source or proposal changed during validation")
                if (core.file_hash(worker) != worker_hash or core.file_hash(Path(__file__)) != attempt["controllerSha256"]
                        or core.file_hash(Path(__file__).with_name("refresh_process_limits.py")) != attempt["resourceLimiterSha256"]):
                    raise core.RefreshError("Trusted parser validation implementation changed during testing")
                attempt.update(status=result["status"], finishedUtc=core.utc(), result=result)
                evidence_file = folder / ("validation-" + str(number) + ".json")
                core.atomic_json(evidence_file, attempt)
                request["validationEvidence"] = {"file": evidence_file.name, "sha256": core.file_hash(evidence_file)}
                core.atomic_json(folder / "request.json", request)
                receipt.update(status="exited", exitCode=process.returncode, finishedUtc=core.utc())
                core.atomic_json(receipt_file, receipt)
                return attempt
        except BaseException as exc:
            if process is not None and process.poll() is None:
                process.kill(); process.communicate()
            attempt.update(status="incomplete" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed",
                           finishedUtc=core.utc(), errorCode="PARSER_VALIDATION_FAILED")
            request.pop("validationEvidence", None)
            core.atomic_json(folder / "request.json", request)
            if process is not None:
                receipt.update(status="exited", exitCode=process.returncode, finishedUtc=core.utc())
                core.atomic_json(receipt_file, receipt)
            raise
        finally:
            if close_limits is not None:
                close_limits()


def validate_for_run(root, run_id, request_id, regression_fixture, **options):
    from refresh_acquire import _compatible
    directory, manifest, stage = core.load_run(root, run_id)
    _compatible(manifest, repair.SOURCE)
    with core.lock(directory):
        _, manifest, stage = core.load_run(root, run_id)
        _compatible(manifest, repair.SOURCE)
        core.check_inputs(stage, manifest)
        try:
            return validate(stage, request_id, regression_fixture, **options)
        finally:
            repair._hash(request_id)
            file = stage / "data/refresh/repair_requests" / request_id / "request.json"
            if file.is_file():
                manifest.setdefault("repairRequests", {})[request_id] = {
                    "status": core.read_json(file).get("status"), "requestSha256": core.file_hash(file)}
                core.save_run(directory, manifest)
