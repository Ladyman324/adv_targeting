"""Retire verified, superseded workspaces from this user's local refresh folder."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

import refresh_core as core


UNPUBLISHED_STATES = {"candidate_ready", "failed", "validation_failed", "prepare_failed"}


@dataclass
class Workspace:
    directory: Path
    run: dict
    stage: Path
    published: dt.datetime | None


def _workspace(root, run_id):
    directory, run, stage = core.load_run(root, run_id)
    publication = directory / "publication"
    journal = publication / "automatic.json"
    state = core.read_json(journal) if journal.is_file() else {}
    published = None
    if (state.get("status") == "completed" and run.get("status") == "candidate_ready"
            and state.get("candidateHash") == run.get("candidate", {}).get("contentHash")
            and state.get("baseline", {}).get("verified")
            and (publication / "receipt.json").is_file()):
        published = dt.datetime.fromisoformat(state["publishedUtc"].replace("Z", "+00:00"))
    return Workspace(directory, run, stage, published)


def _junction(path):
    details = path.lstat()
    return path.is_symlink() or bool(getattr(details, "st_file_attributes", 0)
                                    & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _local_workspaces(root, parent):
    for path in sorted(parent.iterdir()):
        if not path.is_dir() or _junction(path):
            continue
        marker = path / ".refresh-workspace.json"
        if not marker.is_file() or _junction(marker):
            continue
        try:
            value = core.read_json(marker)
            item = _workspace(root, value["runId"])
        except (OSError, ValueError, KeyError, core.RefreshError):
            continue
        if item.stage == path:
            yield item


def _verified_inputs(protected):
    paths = {}
    for item in protected:
        for rel, fact in item.run["inputs"].items():
            paths.setdefault((fact["sha256"], fact["bytes"]), []).append(
                core.contained(item.stage, rel))
    verified = set()
    for (sha, size), choices in paths.items():
        if any(p.is_file() and p.stat().st_size == size and core.file_hash(p) == sha for p in choices):
            verified.add((sha, size))
    return verified


def _superseded(root, item, current, retained_inputs):
    publication = item.directory / "publication"
    if (item.run.get("status") not in UNPUBLISHED_STATES
            or (publication / "automatic.json").exists() or (publication / "receipt.json").exists()
            or not item.run.get("inputs") or item.run.get("createdUtc", "") >= current.run["createdUtc"]
            or not item.run.get("codeCommit")):
        return False
    if any((fact["sha256"], fact["bytes"]) not in retained_inputs
           for fact in item.run["inputs"].values()):
        return False
    result = subprocess.run(["git", "-c", f"safe.directory={root.as_posix()}", "merge-base",
                             "--is-ancestor", item.run["codeCommit"], current.run["codeCommit"]],
                            cwd=str(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def _remove(root, parent, item, protected_stages, current_record, registered):
    with core.lock(item.directory):
        record = core.read_json(registered)
        if record != current_record:
            raise core.RefreshError("Published baseline changed during local retirement")
        path = item.stage
        if (not path.is_absolute() or path.parent != parent or path.resolve(strict=True) != path
                or path in protected_stages or _junction(path)):
            raise core.RefreshError("Workspace retirement path is outside the approved local scope")
        latest = _workspace(root, item.run["id"])
        if latest != item or _junction(path / ".refresh-workspace.json"):
            raise core.RefreshError("Workspace retirement evidence changed")
        for folder, dirs, _ in os.walk(path, followlinks=False):
            if any(_junction(Path(folder) / name) for name in dirs):
                raise core.RefreshError("Workspace contains a linked directory")
        shutil.rmtree(path)


def maintain(root, *, apply=False, expected_run_id=None):
    """Keep the latest two local publications and the active rollback pair."""
    local_data = os.environ.get("LOCALAPPDATA")
    if os.name != "nt" or not local_data:
        return {"status": "no_local_workspaces"}
    chosen = Path(local_data) / "EIC/AdvisorRefresh/workspaces"
    if not chosen.is_dir():
        return {"status": "no_local_workspaces"}
    if _junction(chosen):
        raise core.RefreshError("The local workspace root is linked")
    parent = chosen.resolve(strict=True)
    root = Path(root).resolve()
    publishing = root / "data/refresh/publishing"
    registered = publishing / "current.json"
    with core.lock(publishing / "workspace-retention"):
        record = core.read_json(registered)
        if (record.get("blocked") or not record.get("baseline", {}).get("verified")
                or (expected_run_id is not None and record["run_id"] != expected_run_id)):
            raise core.RefreshError("Cleanup requires the verified published baseline")
        current = _workspace(root, record["run_id"])
        journal = core.read_json(current.directory / "publication/automatic.json")
        backup = core.read_json(Path(journal["backup"]))["previous"]
        if Path(backup["root"]).resolve() != root:
            raise core.RefreshError("The previous publication belongs to a different checkout")
        previous = _workspace(root, backup["run_id"])
        local = list(_local_workspaces(root, parent))
        published = sorted((item for item in local if item.published), key=lambda x: x.published, reverse=True)
        if not current.published or not previous.published or current.published <= previous.published:
            raise core.RefreshError("The live and rollback workspaces must both be verified")
        kept = {item.stage: item for item in [current, previous, *published[:2]]}

        old = [item for item in published if item.stage not in kept and item.published < previous.published]
        unpublished = [item for item in local if item.stage not in kept and not item.published
                       and item.run.get("status") in UNPUBLISHED_STATES]
        retained_inputs = _verified_inputs(kept.values()) if unpublished else set()
        eligible = [(item, "older_publication") for item in old]
        eligible.extend((item, "superseded_unpublished_build") for item in unpublished
                        if _superseded(root, item, current, retained_inputs))

        from refresh_publish_azure import archive_bundle
        if eligible:
            for item in kept.values():
                archive_bundle(root, item.run["id"])

        report = {"status": "completed" if apply else "preview",
                  "utc": core.utc(), "runId": current.run["id"], "rollbackRunId": previous.run["id"],
                  "kept": sorted(item.run["id"] for item in kept.values()), "workspaces": []}
        before = shutil.disk_usage(parent).free
        for item, reason in eligible:
            entry = {"runId": item.run["id"], "workspace": str(item.stage), "reason": reason}
            try:
                if item.run["status"] == "candidate_ready":
                    archive_bundle(root, item.run["id"])
                if apply:
                    _remove(root, parent, item, kept, record, registered)
                entry["status"] = "removed" if apply else "eligible"
            except (OSError, ValueError, KeyError, core.RefreshError) as error:
                entry.update(status="held", errorType=type(error).__name__)
            report["workspaces"].append(entry)
        report["reclaimedBytes"] = max(0, shutil.disk_usage(parent).free - before) if apply else 0
        if apply:
            core.atomic_json(current.directory / "publication/workspace-retention.json", report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(maintain(args.root, apply=args.apply), indent=2))


if __name__ == "__main__":
    main()
