"""Prepare a bounded post-deployment attestation; never publish or alter bundles.

The operator supplies observed runtime identities after separately approved live
verification. This helper verifies the clean candidate locally, writes a separate
receipt, and does not contact Azure or mutate Function App settings.
"""
from __future__ import annotations
import datetime as dt
import json
import re
import refresh_core as core
import refresh_release

SETTING = "DATA_FRESHNESS_PUBLICATION_RECEIPT_JSON"
SHA = re.compile(r"^[a-f0-9]{64}$")
COMMIT = re.compile(r"^[a-f0-9]{40}$")
SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _time(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
        raise core.RefreshError("Publication time requires an ISO timestamp with explicit timezone")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError as exc:
        raise core.RefreshError("Invalid publication timestamp") from exc
    if parsed > dt.datetime.now(dt.timezone.utc):
        raise core.RefreshError("Publication time cannot be in the future")
    return parsed.isoformat().replace("+00:00", "Z")


def prepare_receipt(root, run_id, *, published_utc, api_commit, static_fingerprint,
                    recipient_registry_hash, confirm_runtime_verified=False):
    if confirm_runtime_verified is not True:
        raise core.RefreshError("Explicit confirmation of completed runtime verification is required")
    published = _time(published_utc)
    if not isinstance(api_commit, str) or not COMMIT.fullmatch(api_commit):
        raise core.RefreshError("Observed API commit must be a full lowercase Git SHA")
    if not isinstance(recipient_registry_hash, str) or not SHA.fullmatch(recipient_registry_hash):
        raise core.RefreshError("Observed recipient registry hash must be a full SHA-256")
    if not isinstance(static_fingerprint, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", static_fingerprint):
        raise core.RefreshError("Observed static fingerprint is invalid")
    directory, _, _ = core.load_run(root, run_id)
    with core.lock(directory):
        verified = refresh_release.verify_release(root, run_id, production=True)
        _, _, stage = core.load_run(root, run_id)
        descriptor = core.read_json(stage / "api/shared/data-freshness.json")
        candidate = descriptor.get("candidate") or {}
        candidate_manifest = core.read_json(directory / "candidate/release.json")
        if descriptor.get("schemaVersion") != 1 or candidate.get("id") != run_id:
            raise core.RefreshError("Freshness descriptor does not identify this candidate")
        if (candidate.get("codeCommit") != candidate_manifest.get("codeCommit")
                or api_commit != candidate.get("codeCommit")):
            raise core.RefreshError("Observed API commit does not match the verified candidate")
        if static_fingerprint != candidate.get("staticDataFingerprint") or not static_fingerprint:
            raise core.RefreshError("Observed static fingerprint does not match the verified candidate")
        if recipient_registry_hash != candidate.get("recipientRegistryHash"):
            raise core.RefreshError("Observed recipient registry hash does not match the verified candidate")
        sources = descriptor.get("sources")
        if not isinstance(sources, list):
            raise core.RefreshError("Freshness descriptor has no source inventory")
        source_hashes = {}
        for row in sources:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not SOURCE_ID.fullmatch(row["id"]):
                raise core.RefreshError("Invalid source identity in freshness descriptor")
            value = row.get("sha256")
            if value is None:
                continue
            if not isinstance(value, str) or not SHA.fullmatch(value) or row["id"] in source_hashes:
                raise core.RefreshError("Invalid or duplicate source hash in freshness descriptor")
            source_hashes[row["id"]] = value
        if not source_hashes:
            raise core.RefreshError("No included source snapshots can be attested")
        receipt = {"verified": True, "publishedUtc": published, "candidateId": run_id,
                   "staticDataFingerprint": static_fingerprint, "recipientRegistryHash": recipient_registry_hash,
                   "apiCommit": api_commit, "sourceHashes": source_hashes}
        if len(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")) > 64 * 1024:
            raise core.RefreshError("Publication receipt exceeds the backend setting bound")
        if (directory / "publication").is_symlink() or (directory / "publication/receipt.json").is_symlink():
            raise core.RefreshError("Publication receipt symlinks are not supported")
        path = core.contained(directory, "publication/receipt.json")
        core.atomic_json(path, receipt)
    return {"runId": run_id, "receiptFile": str(path), "settingName": SETTING,
            "candidateContentHash": verified["contentHash"], "settingChanged": False, "publishAuthorized": False,
            "instructions": "After separately approved deployment and verified runtime checks, set this JSON as the backend Function App setting. This command has not changed Azure or the immutable candidate."}
