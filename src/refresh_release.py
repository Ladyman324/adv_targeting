"""Validate, compare and package refresh candidates; never publish them."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

from refresh_core import (RefreshError, atomic_json, check_inputs, code_files, digest,
                          contained, execute_command, facts, file_hash, load_run, lock,
                          paths_matching, read_json, save_run, utc, operation)

ARTIFACT_PATTERNS = ["webapp", "api/shared/act_contacts.json", "api/shared/act_mail_codes.json",
    "api/shared/approved-recipient-release.json", "api/shared/data-freshness.json",
    "data/identity/approved_recipients_manifest.json", "data/identity/approved_recipients_shards",
    "data/identity/approved_recipients.json.gz", "data/output/advisor_emails.json.gz"]


REQUIRED_PRIVATE = ("api/shared/act_contacts.json", "api/shared/act_mail_codes.json",
    "api/shared/approved-recipient-release.json", "data/identity/approved_recipients_manifest.json",
    "data/identity/approved_recipients.json.gz", "data/output/advisor_emails.json.gz")


def _private_dependencies(stage):
    """Missing generated private dependencies must not disappear from facts()."""
    missing = [name for name in REQUIRED_PRIVATE if not (stage / name).is_file()]
    if missing:
        raise RefreshError("Required private dependency is missing: " + ", ".join(missing))
    manifest = read_json(stage / "data/identity/approved_recipients_manifest.json")
    shards = manifest.get("shards")
    if not isinstance(shards, dict) or not shards:
        raise RefreshError("Required recipient shard manifest is empty or invalid")
    selected = {}
    for info in shards.values():
        blob = info["blob"]
        contained(stage, blob)  # reject traversal/absolute declarations
        source = stage / "data/identity/approved_recipients_shards" / Path(blob).name
        if not source.is_file():
            raise RefreshError("Required recipient shard is missing: " + Path(blob).name)
        selected[blob] = source
    return selected


def _module(stage, name):
    spec = importlib.util.spec_from_file_location("_refresh_" + name, str(stage / "src" / (name + ".py")))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _built(manifest):
    if manifest.get("status") not in {"built", "validated", "candidate_ready", "validation_failed", "report_failed", "prepare_failed"}:
        raise RefreshError("Build must finish before validation or release preparation")
    if not manifest.get("plan") or any(manifest["steps"].get(s["id"], {}).get("status") != "success" for s in manifest["plan"]):
        raise RefreshError("Missing or unsuccessful build step")


def check_comparison(comparison, drop_percent=20):
    """Missing optional rankings are reportable, not fatal to unrelated refreshes."""
    optional = {"data/interim/forbes_matches.parquet", "data/interim/barrons_rankings.parquet"}
    missing = [name for name, available in comparison["artifactAvailability"]["after"].items()
               if not available and name not in optional]
    if missing:
        raise RefreshError("Required candidate comparison artifact is missing: " + ", ".join(missing))
    if comparison["assets"]["after"]["issues"]:
        raise RefreshError("EIC asset reconciliation failed; inspect the private comparison report")
    if isinstance(drop_percent, bool) or not isinstance(drop_percent, (int, float)) or not 0 < drop_percent <= 50:
        raise RefreshError("Invalid coverage-drop policy")
    return [key for key, before in comparison["coverage"]["before"].items()
            if key != "fieldUnplaced" and before and comparison["coverage"]["after"][key] is not None
            and comparison["coverage"]["after"][key] < before * (1 - drop_percent / 100)]


def _report(directory, manifest, stage):
    """Compute and bind comparison while the caller holds the run lock."""
    from refresh_reports import compare
    baseline = Path(manifest["baselineRoot"])
    current = facts([baseline / p for p in manifest["baselineArtifacts"]], baseline)
    if current != manifest["baselineArtifacts"]:
        raise RefreshError("The recorded baseline changed; create a new run for a trustworthy comparison")
    result = compare(baseline, stage, directory / "reports", source_statuses=manifest["sources"])
    manifest["comparison"] = {"file": "reports/comparison.json", "sha256": file_hash(directory / "reports/comparison.json"),
                              "coverage": result["coverage"], "semanticContentHash": result["semanticContentHash"]}
    save_run(directory, manifest)
    return result


def report(root, run_id):
    directory, manifest, stage = load_run(root, run_id)
    _built(manifest)
    with lock(directory), operation(directory, manifest, "reporting", success="preserve", failure="report_failed", require=_built):
        return _report(directory, manifest, stage)


def _summary_sources(sources):
    keys = ("id", "label", "sourceDate", "snapshotDate", "edition", "retrievedUtc", "lastSuccessUtc",
            "lastAttemptUtc", "attemptStatus", "status", "records", "sha256", "snapshotId",
            "freshnessDays", "errorCode")
    rows = []
    for source in sources:
        row = {key: source.get(key) for key in keys}
        for field in ("retrievedUtc", "lastSuccessUtc", "lastAttemptUtc"):
            if isinstance(row[field], str):
                row[field] = row[field].replace("+00:00", "Z")
        rows.append(row)
    return rows


def write_descriptor(stage, manifest):
    assets = _module(stage, "web_assets")
    fingerprint = assets.data_fingerprint(stage / "webapp/data")
    registry = read_json(stage / "api/shared/approved-recipient-release.json")
    value = {"schemaVersion": 1, "candidate": {"id": manifest["id"], "createdUtc": manifest["createdUtc"].replace("+00:00", "Z"),
              "codeCommit": manifest["codeCommit"], "staticDataFingerprint": fingerprint,
              "recipientRegistryHash": registry["registryContentHash"]},
             "sources": _summary_sources(manifest["sources"]), "publication": None}
    path = stage / "api/shared/data-freshness.json"
    atomic_json(path, value)
    # Exercise the runtime allowlist as a gate, not just a different Python schema.
    script = "const f=require(process.argv[1]);const x=f.read(process.argv[2]);if(!x.available||x.sources.length!==Number(process.argv[3]))process.exit(2);"
    result = subprocess.run(["node", "-e", script, str(stage / "api/shared/data-freshness.js"), str(path), str(len(value["sources"]))],
                            cwd=str(stage), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RefreshError("Freshness descriptor was rejected by the administrator API schema")
    return value


def validate(root, run_id, coverage_reason=None, run_tests=True):
    directory, manifest, stage = load_run(root, run_id)
    _built(manifest)
    with lock(directory), operation(directory, manifest, "validating", success="validated",
                                     failure="validation_failed", invalidate=("candidate", "validation", "comparison"), require=_built):
        validation = {"startedUtc": utc(), "status": "running", "checks": []}
        manifest["validation"] = validation
        save_run(directory, manifest)
        try:
            check_inputs(stage, manifest)
            _private_dependencies(stage)
            comparison = _report(directory, manifest, stage)
            policy_path = stage / "refresh_policy.json"
            policy = read_json(policy_path) if policy_path.exists() else {}
            drop_percent = policy.get("coverageDropPercent", 20)
            drops = check_comparison(comparison, drop_percent)
            if drops and not (coverage_reason and len(coverage_reason.strip()) >= 12):
                raise RefreshError("Coverage fell by more than %s%% (%s); review and provide an explicit reason" % (drop_percent, ", ".join(drops)))
            if drops:
                validation["coverageOverride"] = {"reason": coverage_reason.strip(), "metrics": drops, "recordedUtc": utc()}
            commands = [
                ("cache_stamps", ["src/web_assets.py"]),
                ("work_locations", ["src/verify_work_locations.py"]),
                ("search", ["src/verify_search_shards.py"]),
                ("web_data", ["src/validate_webapp_data.py"]),
                ("audit", ["src/audit.py"]),
            ]
            if run_tests:
                commands.append(("python_tests", ["-m", "unittest", "discover", "-s", "tests"]))
                tests = sorted(str(p.relative_to(stage)) for p in (stage / "api/test").glob("*.test.js"))
                if not tests:
                    raise RefreshError("API tests are missing from the candidate")
                commands.append(("api_tests", ["node", "--test"] + tests))
            for name, command in commands:
                log = directory / "logs" / ("validate_" + name + ".log")
                execute_command(stage, root, command, log)
                validation["checks"].append({"id": name, "status": "success", "log": log.name, "logSha256": file_hash(log)})
                save_run(directory, manifest)
            write_descriptor(stage, manifest)
            check_inputs(stage, manifest)
            _private_dependencies(stage)
            # Store final bytes, including stamped shells, for release binding.
            artifacts = paths_matching(stage, ARTIFACT_PATTERNS)
            validation.update(status="success", finishedUtc=utc(), runTests=run_tests,
                              artifacts=facts(artifacts, stage))
            validation["artifactHash"] = digest(validation["artifacts"])
            manifest["status"] = "validated"
        except BaseException as exc:
            interrupted = isinstance(exc, (KeyboardInterrupt, SystemExit))
            validation.update(status="incomplete" if interrupted else "failed", finishedUtc=utc(),
                              errorCode="INTERRUPTED" if interrupted else "VALIDATION_FAILED")
            manifest["status"] = "incomplete" if interrupted else "validation_failed"
            raise
        finally:
            save_run(directory, manifest)
    return manifest


def _archive(path, files, base):
    temp = path.with_name(path.name + ".tmp")
    with tarfile.open(str(temp), "w:gz") as archive:
        for source in sorted(files):
            relative = source.relative_to(base)
            if source.is_symlink() or ".." in relative.parts or relative.is_absolute():
                raise RefreshError("Unsafe package member")
            archive.add(str(source), arcname=relative.as_posix(), recursive=False)
    temp.replace(path)


def verify_release(root, run_id, production=False):
    directory, manifest, stage = load_run(root, run_id)
    if manifest.get("status") != "candidate_ready":
        raise RefreshError("No prepared candidate exists")
    candidate = read_json(directory / "candidate/release.json")
    claimed = candidate.pop("contentHash", None)
    if claimed != digest(candidate) or candidate.get("runId") != run_id:
        raise RefreshError("Candidate release manifest integrity failed")
    if claimed != manifest.get("candidate", {}).get("contentHash"):
        raise RefreshError("Candidate no longer matches its pinned release identity")
    if candidate["codeHash"] != manifest["codeHash"] or candidate["validationHash"] != digest(manifest["validation"]):
        raise RefreshError("Candidate no longer matches its code or validation")
    check_inputs(stage, manifest)
    shards = _private_dependencies(stage)
    current = facts(paths_matching(stage, ARTIFACT_PATTERNS), stage)
    if current != manifest["validation"]["artifacts"]:
        raise RefreshError("Validated artifacts changed after validation")
    destination = directory / "candidate"
    for relative, value in candidate["files"].items():
        path = contained(destination, relative)
        if not path.is_file() or file_hash(path) != value["sha256"]:
            raise RefreshError("A release package or dependency changed")
    required_lookups = {"lookups/advisor_emails.json.gz": stage / "data/output/advisor_emails.json.gz"}
    registry = read_json(stage / "api/shared/approved-recipient-release.json")
    required_lookups["lookups/approved_recipients/releases/" + registry["registryContentHash"] + "/manifest.json"] = stage / "data/identity/approved_recipients_manifest.json"
    required_lookups.update({"lookups/" + blob: source for blob, source in shards.items()})
    for relative, source in required_lookups.items():
        packaged = contained(destination, relative)
        if relative not in candidate["files"] or not packaged.is_file() or file_hash(packaged) != file_hash(source):
            raise RefreshError("Required candidate private dependency is missing or changed: " + relative)
    if any(path.is_symlink() for path in destination.rglob("*")):
        raise RefreshError("Candidate symlinks are not supported")
    actual_files = facts([path for path in destination.rglob("*")
                          if path.is_file() and path != destination / "release.json"], destination)
    if actual_files != candidate["files"]:
        raise RefreshError("Candidate file set no longer matches its pinned release")
    if production and (candidate.get("dirty") or not manifest["validation"].get("runTests")):
        raise RefreshError("Production requires committed source and full regression evidence; prepare a fresh clean run after approval")
    return {"verified": True, "runId": run_id, "contentHash": claimed, "dirty": candidate["dirty"],
            "publishAuthorized": False, "files": len(candidate["files"])}


def _validated(manifest):
    if manifest.get("status") not in {"validated", "candidate_ready", "prepare_failed", "incomplete"} or manifest.get("validation", {}).get("status") != "success":
        raise RefreshError("A successful validation is required before release preparation")


def prepare(root, run_id):
    directory, manifest, stage = load_run(root, run_id)
    _validated(manifest)
    with lock(directory), operation(directory, manifest, "preparing", success="candidate_ready",
                                     failure="prepare_failed", invalidate=("candidate",), require=_validated):
        check_inputs(stage, manifest)
        shards = _private_dependencies(stage)
        if facts(paths_matching(stage, ARTIFACT_PATTERNS), stage) != manifest["validation"]["artifacts"]:
            raise RefreshError("Candidate changed after validation")
        destination = directory / "candidate"
        destination.mkdir(exist_ok=True)
        api = stage / "api"
        atomic_json(api / "release.json", {"id": run_id, "commit": manifest["codeCommit"], "builtUtc": utc(),
                                         "dirty": manifest["codeDirty"], "workflow": "refresh-candidate"})
        atomic_json(api / "local.settings.json", {"IsEncrypted": False, "Values": {"AzureWebJobsStorage": "", "FUNCTIONS_WORKER_RUNTIME": "node"}})
        api_files = [p for p in api.rglob("*") if p.is_file() and not any(part in {"node_modules", "test", "tools", "__pycache__"} for part in p.relative_to(api).parts)]
        archive = destination / "api.tgz"
        _archive(archive, api_files, api)
        bash = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"
        log_path = directory / "logs/package_verify.log"
        with log_path.open("wb") as log:
            check = subprocess.run([bash, str(stage / "src/build_api.sh"), "--verify", str(archive), file_hash(archive)],
                                   cwd=str(stage), stdout=log, stderr=subprocess.STDOUT)
        if check.returncode:
            raise RefreshError("Existing API archive verifier rejected the candidate; see package_verify.log")
        # Reuse the static source-boundary and authentication checks, but never
        # call the uploader or create a production publication receipt.
        deploy = _module(stage, "deploy_swa")
        deploy.validate_deployment_source()
        deploy.check_config()
        web = stage / "webapp"
        static_files = [p for p in web.rglob("*") if p.is_file() and p.relative_to(web).as_posix() not in {"data/contacts.json", "data/contacts.json.gz"}]
        _archive(destination / "static.tgz", static_files, web)
        # Keep immutable private dependencies beside the archives for explicit
        # shards-first deployment, never inside the public static package.
        registry = read_json(stage / "api/shared/approved-recipient-release.json")
        for blob, source in shards.items():
            target = contained(destination, "lookups/" + blob)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(target))
        manifest_target = destination / "lookups/approved_recipients/releases" / registry["registryContentHash"] / "manifest.json"
        shutil.copy2(str(stage / "data/identity/approved_recipients_manifest.json"), str(manifest_target))
        email_index = stage / "data/output/advisor_emails.json.gz"
        shutil.copy2(str(email_index), str(destination / "lookups/advisor_emails.json.gz"))
        candidate = {"schemaVersion": 1, "runId": run_id, "createdUtc": utc(), "codeCommit": manifest["codeCommit"],
                     "codeHash": manifest["codeHash"], "dirty": manifest["codeDirty"], "publishAuthorized": False,
                     "validationHash": digest(manifest["validation"]), "sources": manifest["sources"],
                     "files": facts([p for p in destination.rglob("*") if p.is_file() and p != destination / "release.json"], destination)}
        atomic_json(destination / "release.json", {**candidate, "contentHash": digest(candidate)})
        manifest["status"] = "candidate_ready"
        manifest["candidate"] = {"manifest": "candidate/release.json", "contentHash": digest(candidate), "createdUtc": candidate["createdUtc"]}
        save_run(directory, manifest)
        verified = verify_release(root, run_id)
    return {**verified, "status": manifest["status"], "outcome": manifest["outcome"]}
