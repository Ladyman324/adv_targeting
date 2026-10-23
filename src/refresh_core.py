"""Isolated, resumable refresh execution. No publishing or credential storage."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

MODES = ("act-only", "selected-firm", "sec-only", "rankings-only", "full")
RUN_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$")
FORBIDDEN = {".git", "node_modules", "__pycache__", ".chrome-debug-profile", ".playwright"}
ROOT_FILES = {"config.py", "run.py", "serve.py", "territories.yaml", "requirements.txt", "README.md", "refresh_policy.json"}


class RefreshError(RuntimeError):
    pass


@contextlib.contextmanager
def operation(directory, manifest, running, success=None, failure=None, invalidate=(), require=None):
    """Persist an accepted operation, including failures before/after child steps.

    Call only after scope/status rejection checks and while holding the run lock.
    Read-only commands must not use this boundary.
    """
    # load_run precedes the nonblocking lock. Another operation may finish
    # between that read and lock acquisition; never use its obsolete manifest.
    current = read_json(directory / "run.json")
    manifest.clear()
    manifest.update(current)
    if require is not None:
        require(manifest)
    prior_status = manifest.get("status")
    prior_errors = {key: manifest[key] for key in ("failedStep", "errorCode", "recovery") if key in manifest}
    prior_outcome = manifest.get("outcome")
    if prior_status == "failed" or str(prior_status).endswith("_failed"):
        prior_outcome = "failed"
    elif prior_status == "incomplete":
        prior_outcome = "incomplete"
    for key in ("failedStep", "errorCode", "recovery"):
        manifest.pop(key, None)
    for key in invalidate:
        manifest.pop(key, None)
    manifest.update(status=running, outcome="incomplete")
    save_run(directory, manifest)
    try:
        yield
    except BaseException as exc:
        interrupted = isinstance(exc, (KeyboardInterrupt, SystemExit))
        for key in ("candidate", "comparison"):
            manifest.pop(key, None)
        validation = manifest.get("validation", {})
        if validation.get("status") == "success":
            manifest.pop("validation", None)
        elif validation.get("status") == "running":
            validation.update(status="incomplete" if interrupted else "failed", finishedUtc=utc(),
                              errorCode="INTERRUPTED" if interrupted else "VALIDATION_FAILED")
        manifest.update(status="incomplete" if interrupted else (failure or "failed"),
                        outcome="incomplete" if interrupted else "failed",
                        errorCode="INTERRUPTED" if interrupted else manifest.get("errorCode", running.upper() + "_FAILED"))
        save_run(directory, manifest)
        raise
    else:
        if success is not None:
            manifest["status"] = prior_status if success == "preserve" else success
        manifest["outcome"] = (prior_outcome if success == "preserve" and prior_outcome in {"failed", "incomplete"}
                               else "success_with_warnings" if manifest.get("warnings") else "success")
        for key in ("failedStep", "errorCode", "recovery"):
            manifest.pop(key, None)
        if success == "preserve" and prior_outcome in {"failed", "incomplete"}:
            manifest.update(prior_errors)
            manifest.pop("candidate", None)
            if manifest.get("validation", {}).get("status") == "success":
                manifest.pop("validation", None)
        save_run(directory, manifest)


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temp), str(path))


def contained(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise RefreshError("Path escapes its refresh workspace")
    if path == root:
        raise RefreshError("A file or named child directory is required")
    return path


def process_identity(pid):
    """Read OS liveness and creation identity without sending a process signal."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            if ctypes.get_last_error() != 87:  # ERROR_INVALID_PARAMETER: PID no longer exists
                raise RefreshError("Cannot verify child process liveness; do not restart it")
            return {"alive": False, "created": None}
        try:
            code = wintypes.DWORD()
            created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return {"alive": False, "created": None}
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
                raise RefreshError("Cannot verify running process identity")
            return {"alive": True, "created": (created.dwHighDateTime << 32) | created.dwLowDateTime}
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
        stat = Path("/proc") / str(pid) / "stat"
        return {"alive": True, "created": stat.read_text().split(")", 1)[1].split()[19] if stat.exists() else str(pid)}
    except (ProcessLookupError, FileNotFoundError):
        return {"alive": False, "created": None}


def active_children(directory):
    live = []
    for path in (Path(directory) / "logs").glob("*.process.json"):
        item = read_json(path)
        if item.get("status") != "running":
            continue
        actual = process_identity(item["pid"])
        if actual["alive"] and actual["created"] == item.get("created"):
            live.append({"pid": item["pid"], "log": item["log"], "startedUtc": item["startedUtc"]})
    return live


@contextlib.contextmanager
def lock(run_dir):
    """OS-owned byte lock: process death releases it, without stale PID guesses."""
    path = Path(run_dir) / ".operation.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, 2)
    if not handle.tell():
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (OSError, IOError) as exc:
            raise RefreshError("Another operation owns this refresh run; do not restart it") from exc
        if active_children(run_dir):
            raise RefreshError("A verified child is still running; inspect status and wait, do not restart it")
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def code_files(root):
    root = Path(root)
    paths = [root / name for name in ROOT_FILES if (root / name).is_file()]
    for top in ("src", "scripts", "tests", "docs", "api", "webapp"):
        base = root / top
        if not base.exists():
            continue
        for current, dirs, names in os.walk(str(base)):
            dirs[:] = [d for d in dirs if d not in FORBIDDEN and not (top == "webapp" and d == "data")]
            for name in names:
                path = Path(current) / name
                if name.startswith(".env") or name == "local.settings.json" or path.suffix.lower() not in {
                    ".py", ".js", ".cjs", ".json", ".webmanifest", ".css", ".html", ".sh", ".md", ".txt", ".yaml", ".yml", ".jpg", ".png", ".svg", ".ico", ".woff", ".woff2"}:
                    continue
                if name in {"act_contacts.json", "act_mail_codes.json", "data-freshness.json", "approved-recipient-release.json", "release.json"}:
                    continue
                if path.is_symlink():
                    raise RefreshError("Source symlinks are not supported")
                paths.append(path)
    return sorted(set(paths), key=lambda p: p.relative_to(root).as_posix())


def normalized_code_hash(path, relative):
    """Permit only documented generated cache stamps, not arbitrary shell edits."""
    content = Path(path).read_bytes()
    if relative in {"webapp/app.js", "webapp/field.js", "webapp/sw.js", "webapp/index.html", "webapp/field.html"}:
        text = content.decode("utf-8")
        text = re.sub(r'(const (?:DATA_VERSION|VERSION) = ")[^"\r\n]*(";)', r'\1<generated>\2', text)
        text = re.sub(r'((?:app\.js|style\.css|field\.js|field\.css|email\.js|email\.css|dial\.js|training\.js|list_import\.js)\?v=)[A-Za-z0-9._-]+', r'\1<generated>', text)
        content = text.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def facts(paths, root):
    paths = sorted(set(Path(p) for p in paths))
    def one(path):
        # SMB metadata round trips belong in the same bounded worker pool as
        # hashing, not a serialized preflight over thousands of staged files.
        if not path.is_file():
            return None
        if path.is_symlink():
            raise RefreshError("Input symlink refused")
        return path.relative_to(root).as_posix(), {"sha256": file_hash(path), "bytes": path.stat().st_size}
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(result for result in pool.map(one, paths) if result is not None)


def paths_matching(root, patterns):
    found = set()
    for pattern in patterns:
        for path in Path(root).glob(pattern):
            if path.is_file():
                found.add(path)
            elif path.is_dir():
                found.update(p for p in path.rglob("*") if p.is_file())
    return sorted(found)


def source_paths(root, mode=None):
    """Retain all roster evidence and the ACT preference history, not the stale Excel donor."""
    result = []
    for directory in ("data/raw", "data/reference", "data/firm_api", "data/firm_owners", "data/refresh/ranking_editions", "data/refresh/source_archive", "data/refresh/supporting_inputs"):
        if mode in {"act-only", "selected-firm", "rankings-only"} and directory in {"data/firm_api", "data/firm_owners"}:
            continue  # only SEC/full reduces these raw reference caches
        base = Path(root) / directory
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.name.startswith("CRM_Contacts_") or path.suffix.lower() == ".pdf":
                continue
            if directory == "data/raw" and path.name in {"barrons_rankings.json", "forbes_rankings.json"}:
                continue  # derived union; immutable editions are pinned separately
            if "__pycache__" not in path.parts:
                result.append(path)
    decisions = Path(root) / "data/identity/act_identity_decisions.json"
    if decisions.exists():
        result.append(decisions)
    receipts = Path(root) / "data/refresh/source_receipts.json"
    if receipts.exists():
        result.append(receipts)
    for relative in ("data/interim/contact_work_geocodes.csv",
                     "data/interim/contact_work_google_cache.jsonl", "webapp/data/geo_index.json"):
        path = Path(root) / relative
        if path.is_file():
            result.append(path)
    if mode in {"act-only", "selected-firm", "rankings-only"}:
        # These reused SEC tables are genuine inputs, not outputs of this mode.
        # Pin them too: changed cached evidence must invalidate resumed matches.
        for folder in ("data/interim", "data/output"):
            for name in ("advisors", "advisor_branches", "advisor_employments", "advisor_other_names",
                         "advisor_employment_history", "advisor_exams", "advisor_prior_registrations",
                         "firm_state_registrations", "firm_websites", "firm_website_primary",
                         "firm_other_names", "firm_owners", "firms", "firms_scored"):
                path = Path(root) / folder / (name + ".parquet")
                if path.is_file():
                    result.append(path)
    return result


def step(name, command, dependencies=(), outputs=(), sources=()):
    return {"id": name, "command": list(command), "dependencies": list(dependencies),
            "outputs": list(outputs), "sources": list(sources)}


def build_steps(mode, act_file="", rankings=()):
    if mode not in MODES:
        raise RefreshError("Unsupported refresh mode")
    steps = []
    if mode in {"sec-only", "full"}:
        steps += [step("sec_individuals", ["src/parse_advisors.py"], outputs=["data/interim/advisors.parquet", "data/interim/advisor_employments.parquet"], sources=["sec_individual"]),
                  step("sec_branches", ["src/parse_branches.py"], outputs=["data/interim/advisor_branches.parquet"], sources=["sec_individual"]),
                  step("sec_registrations", ["src/parse_state_regs.py"], outputs=["data/interim/firm_state_registrations.parquet"], sources=["sec_firm"]),
                  step("sec_websites", ["src/websites.py"], outputs=["data/interim/firm_websites.parquet"], sources=["sec_firms"]),
                  step("score_firms", ["run.py"], ["sec_individuals", "sec_websites"], ["data/interim/firms_scored.parquet"], ["sec_adv"]),
                  step("firm_reference", ["src/refresh_firm_reference.py", "--root", "."], ["score_firms"],
                       ["data/interim/firm_other_names.parquet", "data/interim/firm_owners.parquet", "data/interim/firm_reference_refresh.json"]),
                  step("export_sec", ["src/export.py"], ["sec_individuals", "sec_branches", "sec_registrations", "firm_reference"], ["data/output/advisors.parquet", "data/output/advisor_branches.parquet", "data/output/firms.parquet"])]
    first = ["export_sec"] if steps else []
    steps += [step("crosswalk", ["src/act_crosswalk.py"], first, ["data/interim/act_crosswalk.parquet"]),
              step("identity", ["src/build_identity_ledger.py"], ["crosswalk"], ["data/identity/identity_manifest.json", "data/identity/act_identity_links.parquet"]),
              step("corrections", ["src/export_act_crd_corrections.py"], ["identity"], ["data/identity/act_crd_corrections.xlsx"]),
              step("economic_links", ["src/build_act_economic_links.py"], ["identity"], ["data/identity/act_economic_manifest.json", "data/identity/act_economic_links.parquet"]),
              step("contacts", ["src/build_contacts.py"], ["identity"], ["webapp/data/contacts.json", "webapp/data/contacts_base.json"]),
              step("geography", ["src/refresh_geography.py", "--root", "."], ["contacts"], ["data/interim/branch_geocoded_*.parquet"]),
              step("working_locations", ["src/contact_work_locations.py"], ["geography"], ["data/interim/contact_work_branches.parquet"]),
              step("placement", ["src/placement.py"], ["working_locations"], ["data/interim/advisor_placement.parquet"]),
              step("state_pins", ["@state_pins"], ["placement"], ["webapp/data/pins_*.json"]),
              step("national", ["src/export_national.py"], ["state_pins"], ["webapp/data/offices_national.json", "webapp/data/metadata.json"]),
              step("display_names", ["src/reconcile_display_names.py"], ["national"], ["webapp/data/advisor_index.json"]),
              step("assets", ["src/build_act_assets.py"], ["economic_links", "display_names"], ["webapp/data/act_assets.json"]),
              step("firm_profiles", ["src/export_firm_profiles.py"], ["display_names"], ["webapp/data/firm_profiles.json"]),
              step("advisor_history", ["src/export_advisor_history.py"], ["display_names"], ["webapp/data/history"])]
    previous = ["firm_profiles"]
    for provider in rankings:
        steps.append(step("rankings_" + provider, ["@rankings", provider], previous,
                          ["data/interim/" + provider + "_rankings.parquet"], ["rankings_" + provider]))
        if provider == "forbes":
            steps.append(step("match_forbes", ["src/forbes_match.py"], ["rankings_forbes", "firm_profiles"], ["data/interim/forbes_matches.parquet"]))
            dependency = "match_forbes"
        else:
            dependency = "rankings_barrons"
        steps.append(step("export_" + provider, ["src/export_" + provider + ".py"], [dependency, "display_names"], ["webapp/data/" + provider + ".json"]))
        previous = ["export_" + provider]
    steps += [step("field_tiles", ["src/build_field_tiles.py"], ["assets", "advisor_history"] + previous, ["webapp/data/tiles", "webapp/data/tile_index.json"]),
              step("field_search", ["src/build_name_index.py"], ["field_tiles"], ["webapp/data/names", "webapp/data/name_index.json"]),
              step("desktop_search", ["src/build_advisor_search.py"], ["display_names", "contacts"], ["webapp/data/search", "webapp/data/advisor_search.json"]),
              step("act_lookup", ["src/build_act_lookup.py"], ["identity", "contacts"], ["api/shared/act_contacts.json"]),
              step("mail_codes", ["src/build_act_mail_codes.py", act_file], ["act_lookup"], ["api/shared/act_mail_codes.json"]),
              step("recipients", ["src/export_approved_recipients.py"], ["mail_codes", "contacts"], ["api/shared/approved-recipient-release.json", "data/identity/approved_recipients_manifest.json", "data/identity/approved_recipients.json.gz"]),
              step("activity_emails", ["src/export_advisor_emails.py"], ["contacts"], ["data/output/advisor_emails.json.gz"])]
    if mode == "rankings-only":
        selected = {"field_tiles", "field_search"}
        selected.update("rankings_" + provider for provider in rankings)
        selected.update("export_" + provider for provider in rankings)
        if "forbes" in rankings:
            selected.add("match_forbes")
        steps = [{**item, "dependencies": [dep for dep in item["dependencies"] if dep in selected]}
                 for item in steps if item["id"] in selected]
    # Enforce topological execution even when steps were assembled by adapters.
    done = set()
    for item in steps:
        if not set(item["dependencies"]).issubset(done):
            raise RefreshError("Invalid dependency graph at " + item["id"])
        done.add(item["id"])
    return steps


def run_path(root, run_id):
    if not RUN_ID.fullmatch(str(run_id)):
        raise RefreshError("Invalid run ID")
    return Path(root) / "data/refresh/runs" / run_id


def load_run(root, run_id):
    directory = run_path(root, run_id)
    value = read_json(directory / "run.json")
    if value.get("id") != run_id or value.get("schemaVersion") != 1:
        raise RefreshError("Invalid refresh manifest")
    stage = Path(value["workspaceRoot"]).resolve()
    if value.get("phase") in {"capture", "copy"} and not (stage / ".refresh-workspace.json").exists():
        if stage == Path(root).resolve() or (stage.exists() and any(stage.iterdir())):
            raise RefreshError("Unmarked nonempty staging workspace; manual review required")
        return directory, value, stage  # read-only bootstrap; resume creates its marker
    marker = read_json(stage / ".refresh-workspace.json")
    if marker.get("runId") != run_id or marker.get("ownerToken") != value.get("ownerToken"):
        raise RefreshError("Workspace ownership mismatch")
    return directory, value, stage


def save_run(directory, value):
    value["updatedUtc"] = utc()
    atomic_json(Path(directory) / "run.json", value)


def baseline_paths(root):
    result = []
    for directory in ("data/interim", "data/output", "data/identity", "webapp/data"):
        base = Path(root) / directory
        if base.exists():
            result.extend(p for p in base.rglob("*") if p.is_file() and p.suffix not in {".gz", ".xlsx", ".csv"})
    for name in ("act_contacts.json", "act_mail_codes.json"):
        path = Path(root) / "api/shared" / name
        if path.is_file():
            result.append(path)
    for relative in ("data/interim/contact_work_geocodes.csv", "data/interim/geocoded_google.csv", "api/shared/approved-recipient-release.json"):
        if (Path(root) / relative).is_file():
            result.append(Path(root) / relative)
    for relative in ("data/identity/approved_recipients.json.gz", "data/output/advisor_emails.json.gz"):
        if (Path(root) / relative).is_file():
            result.append(Path(root) / relative)
    result.extend((Path(root) / "data/identity").glob("*.csv"))
    result.extend((Path(root) / "data/identity/approved_recipients_shards").rglob("*.gz"))
    return result


def create_run(root, mode, firms=(), stage_parent=None, run_id=None):
    from refresh_stage import start_stage
    return start_stage(root, mode, firms, stage_parent, run_id)


def check_inputs(stage, manifest):
    current = facts(source_paths(stage, manifest.get("mode")), stage)
    if current != manifest["inputs"]:
        raise RefreshError("Pinned source inputs changed; use import/acquire to register a new snapshot")
    if manifest.get("reusedArtifacts") and facts([stage / p for p in manifest["reusedArtifacts"]], stage) != manifest["reusedArtifacts"]:
        raise RefreshError("A pinned reused artifact changed; create a fresh run from a validated baseline")
    current_code = {relative: normalized_code_hash(stage / relative, relative) for relative in manifest["codeFiles"]}
    if current_code != manifest["codeNormalization"]:
        raise RefreshError("Staged implementation changed; create a new run rather than reusing old success")
    if {path.relative_to(stage).as_posix() for path in code_files(stage)} != set(manifest["codeFiles"]):
        raise RefreshError("Staged source file set changed")
    controller_root = Path(manifest["baselineRoot"])
    live_code = {relative: normalized_code_hash(controller_root / relative, relative) for relative in manifest["codeFiles"]}
    if live_code != manifest["codeNormalization"]:
        raise RefreshError("Refresh controller changed since this run was staged; create a new run")


def child_environment(stage, root, marker, online=False):
    env = os.environ.copy()
    if not online:
        for key in list(env):
            if any(part in key.upper() for part in ("PASSWORD", "SECRET", "TOKEN", "CONNECTION_STRING", "API_KEY", "ACCOUNT_KEY", "ACCESS_KEY", "AZUREWEBJOBSSTORAGE")) or key.upper().startswith(("ACT_", "GRAPH_", "AZURE_", "MSI_", "IDENTITY_", "GOOGLE_", "OPENAI_", "SWA_")):
                env.pop(key, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["ADVISOR_REFRESH_OFFLINE"] = "0" if online else "1"
    env["REFRESH_NETWORK_ATTEMPT_FILE"] = str(marker)
    env["PYTHONPATH"] = os.pathsep.join([str(stage / "src/refresh_offline"), str(stage / "src"), str(stage)])
    env["NODE_PATH"] = str(Path(root) / "api/node_modules")
    if not online:
        env["NODE_OPTIONS"] = '--require="' + str(stage / "src/refresh_offline/node.cjs").replace("\\", "/") + '"'
    adv = list((stage / "data/raw").glob("refresh_adv_*"))
    if adv:
        env["ADV_SOURCE"] = str(sorted(adv)[-1])
    elif env.get("ADV_SOURCE"):
        # Never let a staged child read a developer's external download path.
        env.pop("ADV_SOURCE", None)
    return env


def execute_command(stage, root, command, log_path, online=False):
    marker = Path(str(log_path) + ".network-denied")
    if marker.exists():
        marker.unlink()
    env = child_environment(stage, root, marker, online=online)
    executable = "node" if command and command[0] == "node" else sys.executable
    args = command[1:] if executable == "node" else command
    with Path(log_path).open("ab") as log:
        process = subprocess.Popen([executable] + list(args), cwd=str(stage), env=env,
                                   stdout=log, stderr=subprocess.STDOUT, shell=False)
        handle_path = Path(str(log_path) + ".process.json")
        identity = process_identity(process.pid)
        receipt = {"pid": process.pid, "created": identity["created"], "status": "running", "startedUtc": utc(), "log": Path(log_path).name}
        atomic_json(handle_path, receipt)
        rc = process.wait()
        receipt.update(status="exited", exitCode=rc, finishedUtc=utc())
        atomic_json(handle_path, receipt)
    if marker.exists() and marker.stat().st_size:
        raise RefreshError("An offline build attempted network access; see the restricted step log")
    if rc:
        raise RefreshError("Build command failed with exit %s; see %s" % (rc, Path(log_path).name))


def execute_step(stage, root, item, log_path):
    command = item["command"]
    if command[0] == "@state_pins":
        states = sorted(p.stem.rsplit("_", 1)[-1] for p in (stage / "data/interim").glob("branch_geocoded_*.parquet"))
        if not states:
            raise RefreshError("No current geocoded carrier rows exist")
        # Stale states are removed only inside this marked, isolated workspace.
        for path in (stage / "webapp/data").glob("pins_*.json"):
            if path.stem.rsplit("_", 1)[-1] not in states:
                path.unlink()
                gz = path.with_suffix(path.suffix + ".gz")
                if gz.exists():
                    gz.unlink()
        for state in states:
            execute_command(stage, root, ["src/export_geojson.py", state], log_path)
    elif command[0] == "@rankings":
        provider = command[1]
        edition_dir = stage / "data/refresh/ranking_editions" / provider
        files = sorted(str(path.relative_to(stage)) for path in edition_dir.glob("*.json"))
        execute_command(stage, root, ["src/refresh_rankings.py", "--root", ".", "--provider", provider, "--immutable-only"] + files, log_path)
    else:
        execute_command(stage, root, command, log_path)


def build(root, run_id, retry=False, fallback_reason=None):
    from refresh_inventory import inventory, validate_act_snapshot
    directory, manifest, stage = load_run(root, run_id)
    if manifest.get("phase") in {"capture", "copy"}:
        raise RefreshError("Staging is incomplete; use resume --run " + run_id + " to repair it first")
    with lock(directory), operation(directory, manifest, "building",
                                     invalidate=("candidate", "validation", "comparison")):
        check_inputs(stage, manifest)
        validate_act_snapshot(stage)
        if manifest["mode"] == "rankings-only":
            required = ["webapp/data/contacts.json", "webapp/data/advisor_index.json", "webapp/data/firm_profiles.json",
                        "webapp/data/act_assets.json", "webapp/data/advisor_search.json",
                        "api/shared/act_contacts.json", "api/shared/act_mail_codes.json",
                        "api/shared/approved-recipient-release.json", "data/identity/approved_recipients_manifest.json",
                        "data/identity/approved_recipients.json.gz", "data/output/advisor_emails.json.gz",
                        "data/interim/advisor_placement.parquet", "data/interim/advisors.parquet",
                        "data/interim/advisor_employment_history.parquet", "data/output/advisor_branches.parquet",
                        "data/identity/identity_manifest.json", "data/identity/act_identity_links.parquet",
                        "data/identity/act_crd_corrections.csv"]
            missing = [name for name in required if not (stage / name).is_file()]
            if missing or not list((stage / "data/interim").glob("branch_geocoded_*.parquet")):
                raise RefreshError("Rankings-only needs a validated full baseline; missing reused artifacts: " + ", ".join(missing))
        latest_attempts = {a["sourceId"]: a for a in manifest.get("attempts", [])}
        failed_sources = [sid for sid, a in latest_attempts.items() if a.get("status") in {"failed", "incomplete", "running"}]
        if failed_sources:
            if not fallback_reason or len(fallback_reason.strip()) < 12:
                raise RefreshError("A source refresh failed; review last-good reuse with --fallback-reason")
            manifest.setdefault("fallbacks", []).append({"sources": failed_sources, "reason": fallback_reason.strip(), "recordedUtc": utc()})
            manifest.setdefault("warnings", []).append("Explicitly reused last-good sources after failure: " + ", ".join(failed_sources))
        act_files = sorted((stage / "data/raw").glob("act_pull_manifest_*.json"))
        pull = read_json(act_files[-1])
        act_file = "data/raw/act_contacts_" + pull["policies"]["pull_id"] + ".json"
        rankings = [p for p in ("barrons", "forbes") if (stage / "data/raw" / (p + "_rankings.json")).exists()
                    or any((stage / "data/refresh/ranking_editions" / p).glob("*.json"))]
        if manifest["mode"] == "rankings-only" and not rankings:
            raise RefreshError("Requested rankings are missing; import a ranking edition first")
        plan = build_steps(manifest["mode"], act_file, rankings)
        manifest["plan"] = plan
        manifest["status"] = "building"
        save_run(directory, manifest)
        logs = directory / "logs"
        logs.mkdir(exist_ok=True)
        for item in plan:
            dependencies = {key: manifest["steps"][key]["outputHash"] for key in item["dependencies"]}
            fingerprint = digest({"code": manifest["codeHash"], "inputs": manifest["inputs"], "step": item, "dependencies": dependencies})
            old = manifest["steps"].get(item["id"], {})
            outputs = facts(paths_matching(stage, item["outputs"]), stage)
            if old.get("status") == "success" and old.get("fingerprint") == fingerprint and outputs == old.get("finalOutputs", old.get("outputs")):
                continue
            result = {"status": "running", "startedUtc": utc(), "fingerprint": fingerprint,
                      "command": item["command"], "attempt": old.get("attempt", 0) + 1}
            manifest["steps"][item["id"]] = result
            save_run(directory, manifest)
            start = time.monotonic()
            before_stamps = {p.relative_to(stage).as_posix(): (p.stat().st_mtime_ns, p.stat().st_size)
                             for p in paths_matching(stage, item["outputs"])}
            try:
                execute_step(stage, root, item, logs / (item["id"] + ".log"))
                outputs = facts(paths_matching(stage, item["outputs"]), stage)
                missing = [pattern for pattern in item["outputs"] if not paths_matching(stage, [pattern])]
                if missing:
                    raise RefreshError("Step is missing required outputs: " + ", ".join(missing))
                unchanged, evidence = [], []
                for pattern in item["outputs"]:
                    changed = [p.relative_to(stage).as_posix() for p in paths_matching(stage, [pattern])
                               if before_stamps.get(p.relative_to(stage).as_posix()) != (p.stat().st_mtime_ns, p.stat().st_size)]
                    if not changed:
                        unchanged.append(pattern)
                    evidence.extend(changed)
                if unchanged:
                    raise RefreshError("A successful command did not regenerate its declared output: " + ", ".join(unchanged))
                result.update(status="success", outputs=outputs, outputHash=digest(outputs), productionEvidence=sorted(set(evidence)))
            except BaseException as exc:
                interrupted = isinstance(exc, (KeyboardInterrupt, SystemExit))
                result.update(status="incomplete" if interrupted else "failed", errorCode="INTERRUPTED" if interrupted else "STEP_FAILED")
                manifest.update(status=result["status"], outcome=result["status"], errorCode=result["errorCode"], failedStep=item["id"])
                raise
            finally:
                result.update(finishedUtc=utc(), durationSeconds=round(time.monotonic() - start, 3))
                save_run(directory, manifest)
        try:
            check_inputs(stage, manifest)
            for item in plan:
                manifest["steps"][item["id"]]["finalOutputs"] = facts(paths_matching(stage, item["outputs"]), stage)
            from refresh_inventory import candidate_inventory
            source_inventory = inventory(stage)
            manifest.update(status="built", sourceInventory=source_inventory,
                            sources=candidate_inventory(stage, manifest["mode"], source_inventory))
            manifest["outcome"] = "success_with_warnings" if manifest.get("warnings") else "success"
            manifest.pop("failedStep", None)
            manifest.pop("errorCode", None)
        except BaseException as exc:
            interrupted = isinstance(exc, (KeyboardInterrupt, SystemExit))
            manifest.update(status="incomplete" if interrupted else "failed",
                            outcome="incomplete" if interrupted else "failed",
                            errorCode="INTERRUPTED" if interrupted else "FINAL_VERIFICATION_FAILED",
                            failedStep="final_verification")
            raise
        finally:
            save_run(directory, manifest)
    return manifest
