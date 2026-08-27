"""Immutable provenance helpers for private contact-data artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
PULL_ID = re.compile(
    r"^(?:act_contacts|act_eic_contact)_"
    r"(\d{4}-\d{2}-\d{2}(?:T\d{6}Z)?)\.json$"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProvenanceError(RuntimeError):
    """An artifact is incomplete, stale, mismatched or malformed."""


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_pull_id(now=None):
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def pull_id_from_path(path):
    match = PULL_ID.match(Path(path).name)
    if not match:
        raise ProvenanceError(f"not a dated Act artifact: {Path(path).name}")
    return match.group(1)


def sha256_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def atomic_write_bytes(path, value, *, create_only=False):
    """Publish complete bytes atomically.

    ``create_only`` is for immutable raw inputs. The temporary file is fully
    written and fsynced first, then published with a create-only rename on
    Windows or hard link on POSIX. Unlike ``replace()``, either operation fails
    if that name already exists. The temporary file lives beside the target so
    publication cannot cross volumes.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                    dir=str(target.parent))
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(value)
            fh.flush()
            os.fsync(fh.fileno())
        if create_only:
            try:
                # Windows rename is create-only (it raises when target exists)
                # and works on SMB shares where hard-link creation is commonly
                # disabled. POSIX rename replaces, so use an atomic link there.
                if os.name == "nt":
                    temp.rename(target)
                else:
                    os.link(temp, target)
            except FileExistsError as exc:
                raise ProvenanceError(
                    f"immutable artifact already exists: {target}") from exc
            if temp.exists():
                temp.unlink()
        else:
            temp.replace(target)
    except Exception:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path, value, pretty=False, *, create_only=False):
    if pretty:
        data = (json.dumps(value, indent=1, ensure_ascii=False, default=str)
                + "\n").encode("utf-8")
    else:
        data = canonical_json_bytes(value)
    atomic_write_bytes(path, data, create_only=create_only)


def atomic_create_json(path, value, pretty=False):
    """Create an immutable JSON artifact; never replace existing bytes."""
    atomic_write_json(path, value, pretty=pretty, create_only=True)


def atomic_publish_file(source, target):
    """Move one already-complete staged file to an immutable final name."""
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ProvenanceError(f"immutable artifact already exists: {target}")
    try:
        if os.name == "nt":
            source.rename(target)
        else:
            os.link(source, target)
            source.unlink()
    except FileExistsError as exc:
        raise ProvenanceError(
            f"immutable artifact already exists: {target}") from exc


def json_array_facts(path, role="json_array"):
    """Describe a JSON-array artifact without retaining contact values."""
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProvenanceError(f"cannot read {source}: {exc}") from exc
    if not isinstance(payload, list):
        raise ProvenanceError(f"{source.name} is not a JSON array")
    ids = [str(row.get("id") or "").strip()
           for row in payload if isinstance(row, dict)]
    if len(ids) != len(payload) or any(not value for value in ids):
        raise ProvenanceError(f"{source.name} has a row without an Act id")
    if len(set(ids)) != len(ids):
        raise ProvenanceError(f"{source.name} contains duplicate Act ids")
    return {"role": role, "path": source.as_posix(), "file": source.name,
            "sha256": sha256_file(source), "bytes": source.stat().st_size,
            "rows": len(payload), "complete": True}


def json_array_ids(path):
    """Return the exact non-empty unique Act ids in a JSON-array artifact."""
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProvenanceError(f"cannot read {source}: {exc}") from exc
    if not isinstance(payload, list):
        raise ProvenanceError(f"{source.name} is not a JSON array")
    ids = []
    for position, row in enumerate(payload):
        if not isinstance(row, dict):
            raise ProvenanceError(
                f"{source.name} row {position} is not a JSON object")
        contact_id = str(row.get("id") or "").strip()
        if not contact_id:
            raise ProvenanceError(
                f"{source.name} row {position} has no Act id")
        ids.append(contact_id)
    if len(set(ids)) != len(ids):
        raise ProvenanceError(f"{source.name} contains duplicate Act ids")
    return frozenset(ids)


def file_facts(path, role, rows=None, complete=True, extra=None):
    source = Path(path)
    fact = {"role": role, "path": source.as_posix(), "file": source.name,
            "sha256": sha256_file(source), "bytes": source.stat().st_size,
            "complete": bool(complete)}
    if rows is not None:
        fact["rows"] = int(rows)
    if extra:
        fact.update(dict(extra))
    return fact


def validate_owner_artifact(contacts_path, owner_path, require_complete=True):
    """Return a hash-bound owner payload or raise ProvenanceError."""
    contacts, owner = Path(contacts_path), Path(owner_path)
    contact_pull, owner_pull = pull_id_from_path(contacts), pull_id_from_path(owner)
    if contact_pull != owner_pull:
        raise ProvenanceError(
            f"Act pair pull ids differ: {contact_pull} != {owner_pull}")
    if "T" not in contact_pull:
        raise ProvenanceError(
            "legacy date-only Act owner pairs are not exact snapshots; "
            "run src/act_pull.py to create a hash-bound UTC pull")
    try:
        payload = json.loads(owner.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProvenanceError(f"cannot read {owner}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProvenanceError(f"{owner.name} is not a JSON object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ProvenanceError(f"{owner.name} has unsupported schema_version")
    if payload.get("pull_id") != contact_pull:
        raise ProvenanceError(f"{owner.name} records the wrong pull_id")
    if payload.get("contacts_file") != contacts.name:
        raise ProvenanceError(f"{owner.name} records the wrong contacts_file")
    if payload.get("contacts_sha256") != sha256_file(contacts):
        raise ProvenanceError(f"{owner.name} was built from different contact bytes")
    contact_ids = json_array_ids(contacts)
    if int(payload.get("contacts_rows", -1)) != len(contact_ids):
        raise ProvenanceError(f"{owner.name} records the wrong contact row count")
    failed = list(payload.get("failed_codes") or [])
    if require_complete and (payload.get("complete") is not True or failed):
        detail = f"; failed codes: {', '.join(failed)}" if failed else ""
        raise ProvenanceError(f"{owner.name} is incomplete{detail}")
    owners = payload.get("owner_by_contact_id")
    if not isinstance(owners, dict):
        raise ProvenanceError(f"{owner.name} has no owner_by_contact_id map")
    owner_ids = {str(value).strip() for value in owners}
    if "" in owner_ids:
        raise ProvenanceError(f"{owner.name} assigns a blank contact id")
    unknown = owner_ids - contact_ids
    if unknown:
        raise ProvenanceError(
            f"{owner.name} assigns {len(unknown)} ids absent from its contact pull")
    expected_codes = {str(code) for code in payload.get("expected_codes") or []}
    queried_codes = {str(code) for code in payload.get("queried_codes") or []}
    counts = payload.get("counts")
    if not expected_codes:
        raise ProvenanceError(f"{owner.name} does not declare expected codes")
    if queried_codes != expected_codes:
        raise ProvenanceError(f"{owner.name} did not query every expected code")
    if not isinstance(counts, dict):
        raise ProvenanceError(f"{owner.name} has no query counts")
    if set(map(str, counts)) != expected_codes:
        raise ProvenanceError(f"{owner.name} query counts do not cover every code")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
           for value in counts.values()):
        raise ProvenanceError(f"{owner.name} has an invalid query count")
    conflicts = payload.get("conflicts")
    if not isinstance(conflicts, list) or any(
            not isinstance(row, dict) for row in conflicts):
        raise ProvenanceError(f"{owner.name} has malformed conflicts")
    if any(str(code) not in expected_codes for code in owners.values()):
        raise ProvenanceError(f"{owner.name} assigns an unknown owner code")
    conflicted = {str(row.get("id") or "") for row in conflicts}
    if "" in conflicted:
        raise ProvenanceError(f"{owner.name} has a conflict without an Act id")
    conflict_code_count = 0
    for row in conflicts:
        codes = [str(code) for code in row.get("codes") or []]
        if len(set(codes)) < 2 or any(code not in expected_codes for code in codes):
            raise ProvenanceError(f"{owner.name} has malformed conflict codes")
        conflict_code_count += len(set(codes))
    if conflicted & set(owners):
        raise ProvenanceError(f"{owner.name} assigns conflicted contact ids")
    if conflicted - contact_ids:
        raise ProvenanceError(f"{owner.name} has conflicts for unknown contacts")
    if sum(counts.values()) != len(owners) + conflict_code_count:
        raise ProvenanceError(f"{owner.name} query counts do not reconcile")
    return payload


def source_hashes(paths):
    ordered = sorted((Path(path) for path in paths), key=lambda p: p.as_posix())
    return {path.as_posix(): sha256_file(path) for path in ordered}


def build_manifest(inputs, generator_files=(), policies=None, outputs=(),
                   generated_utc=None):
    """Build a deterministic-id manifest without copying source contents."""
    input_rows = sorted((dict(row) for row in inputs),
                        key=lambda row: (str(row.get("role", "")),
                                         str(row.get("path", ""))))
    if not input_rows:
        raise ProvenanceError("a build manifest must name at least one input")
    for row in input_rows:
        if row.get("complete") is not True:
            raise ProvenanceError(
                f"manifest input is not complete: {row.get('path', '?')}")
        if not SHA256.fullmatch(str(row.get("sha256") or "")):
            raise ProvenanceError(
                f"manifest input has no valid SHA-256: {row.get('path', '?')}")
        if not str(row.get("role") or "").strip():
            raise ProvenanceError("manifest input has no role")
    policy = dict(policies or {})
    generator = {"source_hashes": source_hashes(generator_files)}
    identity = {"schema_version": SCHEMA_VERSION, "inputs": input_rows,
                "generator": generator, "policies": policy}
    digest = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:20]
    return {**identity, "build_id": f"contacts-{digest}",
            "generated_utc": generated_utc or utc_now(),
            "outputs": sorted((dict(row) for row in outputs),
                              key=lambda row: str(row.get("path", "")))}


def write_manifest(path, manifest, *, create_only=False):
    validate_manifest(manifest)
    atomic_write_json(Path(path), dict(manifest), pretty=True,
                      create_only=create_only)


def validate_manifest(manifest):
    """Validate structure and recompute the deterministic build id."""
    if not isinstance(manifest, dict):
        raise ProvenanceError("manifest is not a JSON object")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ProvenanceError("manifest has unsupported schema_version")
    inputs = manifest.get("inputs")
    generator = manifest.get("generator")
    policies = manifest.get("policies")
    if not isinstance(inputs, list) or not isinstance(generator, dict) \
            or not isinstance(policies, dict):
        raise ProvenanceError("manifest identity fields are malformed")
    for row in inputs:
        if not isinstance(row, dict) or row.get("complete") is not True:
            raise ProvenanceError("manifest contains an incomplete input")
        if not SHA256.fullmatch(str(row.get("sha256") or "")):
            raise ProvenanceError("manifest contains an invalid input SHA-256")
        if not str(row.get("role") or "").strip():
            raise ProvenanceError("manifest contains an input without a role")
    identity = {"schema_version": SCHEMA_VERSION, "inputs": inputs,
                "generator": generator, "policies": policies}
    expected = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:20]
    if manifest.get("build_id") != f"contacts-{expected}":
        raise ProvenanceError("manifest build_id does not match its identity")
    if not isinstance(manifest.get("outputs", []), list):
        raise ProvenanceError("manifest outputs are malformed")
    return manifest


def load_manifest(path):
    """Load and structurally validate a contact build manifest."""
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProvenanceError(f"cannot read {source}: {exc}") from exc
    return validate_manifest(payload)
