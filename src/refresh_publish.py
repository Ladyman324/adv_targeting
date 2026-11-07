"""Publish exact prepared bundles under an explicit registration policy.

Cloud behavior is supplied by a backend. No candidate or old success marker is
rewritten. Deployment failures restore the retained matching release. Local
post-publication failures remain resumable without redeploying a verified app.
"""
from __future__ import annotations
import json
from pathlib import Path
import refresh_core as core
from refresh_publish_policy import minor_warnings, check_source_results

class PublicationHeld(core.RefreshError): pass

def check_gate(root, run_id):
    from refresh_release import verify_release, check_comparison
    directory,run,stage=core.load_run(root,run_id)
    verified=verify_release(root,run_id,production=True)
    expected={"cache_stamps","work_locations","search","web_data","audit","python_tests","api_tests"}
    if {row["id"] for row in run["validation"]["checks"] if row["status"]=="success"} != expected:
        raise PublicationHeld("Full validation evidence is required")
    warnings=minor_warnings(run.get("warnings",[]))
    workflow=directory/"workflow/summary.json"
    if workflow.exists():check_source_results(core.read_json(workflow).get("sources",{}))
    comparison=core.read_json(directory/"reports/comparison.json")
    policy_path=stage/"refresh_policy.json"
    policy=core.read_json(policy_path) if policy_path.exists() else {}
    drops=check_comparison(comparison,policy.get("coverageDropPercent",20))
    if drops:
        from refresh_publish_policy import minor_map_reason
        reason=minor_map_reason(comparison,drops)
        if not reason:raise PublicationHeld("Major coverage changes require review")
        warnings.append(reason)
    return dict(root=str(Path(root).resolve()),run_id=run_id,directory=str(directory),stage=str(stage),
                candidate_hash=verified["contentHash"],code_commit=run["codeCommit"],warnings=warnings)

def publish(root, run_id, backend=None, config=None, gate=check_gate):
    config=config or {}
    if config.get("enabled") is not True:
        raise PublicationHeld("Automatic publication is not enabled")
    if backend is None:
        from refresh_publish_azure import AzurePublisher
        backend=AzurePublisher(root,config)
    spec=gate(root,run_id)
    folder=Path(spec["directory"])/"publication"
    folder.mkdir(exist_ok=True)
    state_path=folder/"automatic.json"
    with core.lock(folder):
        state=core.read_json(state_path) if state_path.exists() else dict(
            schemaVersion=1,runId=run_id,candidateHash=spec["candidate_hash"],events=[],warnings=spec["warnings"])
        if state["candidateHash"]!=spec["candidate_hash"]:
            raise PublicationHeld("Publication journal belongs to a different candidate")
        if state.get("status")=="completed":return state
        if state.get("status")=="rollback_failed":
            raise PublicationHeld("Failed rollback requires operator recovery")
        def mark(status,**fields):
            state.update(status=status,updatedUtc=core.utc(),**fields)
            state["events"].append(dict(status=status,utc=state["updatedUtc"]))
            core.atomic_json(state_path,state)
        try:
            with backend.lock():
                current=backend.current()
                if state.get("status")=="published_adoption_failed":
                    backend.verify(spec)
                else:
                    partial={"transferring_lookups","lookups_transferred","api_deployed","static_deployed","rolling_back"}
                    if state.get("status") in partial and not backend.is_candidate(current,spec):
                        try:
                            backend.rollback(state["backup"],spec)
                            mark("rolled_back",recovered=True)
                        except Exception as recovery_error:
                            mark("rollback_failed",rollbackErrorType=type(recovery_error).__name__)
                            if hasattr(backend,"block"):backend.block(spec)
                            raise
                        raise PublicationHeld("Interrupted publication restored; review before retry")
                    # Completed remote components can resume receipt/adoption safely.
                    if backend.is_candidate(current,spec):
                        try:
                            backend.verify(spec)
                        except Exception as exc:
                            if state.get("backup"):
                                mark("rolling_back",errorType=type(exc).__name__)
                                try:
                                    backend.rollback(state["backup"],spec)
                                    mark("rolled_back",errorType=type(exc).__name__)
                                except Exception as recovery_error:
                                    mark("rollback_failed",rollbackErrorType=type(recovery_error).__name__)
                                    if hasattr(backend,"block"):backend.block(spec)
                            raise
                        mark("live_verified",recovered=True)
                    else:
                        backend.assert_baseline(current)
                        backend.check_changes(spec)
                        if not backend.quiet():
                            mark("deferred_busy")
                            return state
                        backup=backend.backup(current)
                        mark("backup_ready",backup=backup)
                        try:
                            mark("transferring_lookups")
                            backend.transfer(spec)
                            mark("lookups_transferred")
                            backend.deploy_api(spec)
                            mark("api_deployed")
                            backend.verify_api(spec)
                            backend.deploy_static(spec)
                            mark("static_deployed")
                            backend.verify(spec)
                            mark("live_verified")
                        except Exception as exc:
                            mark("rolling_back",errorType=type(exc).__name__)
                            try:
                                backend.rollback(backup,spec)
                                mark("rolled_back",errorType=type(exc).__name__)
                            except Exception as rollback_error:
                                mark("rollback_failed",errorType=type(exc).__name__,rollbackErrorType=type(rollback_error).__name__)
                                if hasattr(backend,"block"):backend.block(spec)
                            raise
                # A verified live pair is not rolled back for a local-copy failure.
                backend.record_receipt(spec)
                mark("receipt_recorded")
                try:
                    adopted=backend.adopt(spec)
                except Exception:
                    mark("published_adoption_failed")
                    raise
                backend.record_current(spec,adopted)
                mark("completed",publishedUtc=core.utc(),baseline=adopted)
                return state
        except Exception as exc:
            if state.get("status") not in {"rolled_back","rollback_failed","published_adoption_failed"}:
                mark("held",errorType=type(exc).__name__)
            raise
