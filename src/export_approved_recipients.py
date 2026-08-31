"""Export the server-authoritative approved-recipient registry from identity links."""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import math
import os
import pathlib
import re
import sys
from collections import Counter

import pandas as pd

ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
from identity_normalize import clean_text, normalize_crd, normalize_email
from roster_greetings import valid_greeting
from identity_schema import (
    APPROVED_REGISTRY_FILENAME, APPROVED_REGISTRY_GZIP_FILENAME,
    IDENTITY_DIRNAME, LINKS_FILENAME, MANIFEST_FILENAME, content_hash, runtime_content_hash,
)

IDENTITY = ROOT / "data" / IDENTITY_DIRNAME
BLOB_CONTAINER, BLOB_NAME = "lookups", "approved_recipients.json.gz"
SHARD_PREFIX = "approved_recipients"
SHARD_WIDTH = 2
SHARD_DIR = IDENTITY / "approved_recipients_shards"
SHARD_MANIFEST_PATH = IDENTITY / "approved_recipients_manifest.json"
RELEASE_DESCRIPTOR_PATH = (ROOT / "api" / "shared" /
                           "approved-recipient-release.json")
# WHAT MAY BE EXPORTED AND PRESENTED TO THE CONTROLLED COMPOSER.
#
# The registry carries the superset and recipient-registry.js narrows it at run
# time from APPROVED_RECIPIENT_TIERS. Deciding it only here meant the policy
# could not be tightened -- or loosened -- without rebuilding the API, uploading
# a new blob, and matching the two by content hash. Exporting the superset makes
# that an environment setting; the tier travels on every record so the runtime
# can still refuse what it does not accept.
#
#   confirmed  the firm stated this CRD and the SEC record agrees
#   high       a probabilistic roster-to-SEC match, email-authorized by an
#              explicit business decision rather than by proof of identity
#
# The relevant contact calibration currently has 633 labelled rows (0.32% of
# 200,949 contact rows). All 467 labels accepted by the shipping high-tier gate
# were correct. That is encouraging, but the labeller reaches unusually clean
# rows and its own documentation calls the result an optimistic bound. The
# unrelated 0.989 Act first-name-gate statistic is not evidence for this tier.
# `review` remains outside this registry and outside the controlled composer.
EXPORTED_TIERS = frozenset({"confirmed", "high"})
# The weighted matcher sums to 1.0 and can add W_SUFFIX=0.18. Preserve the
# producer's evidence only inside that real model range. This is a score, not a
# probability; clamping an impossible value would turn bad evidence into a lie.
MATCH_SCORE_MAX = 1.18

RELEASE_PROVENANCE_KEYS = (
    "identityManifestHash", "identityLinksSha256", "contactsSha256",
    "actSource", "actSourceSha256",
)


def load_contacts() -> dict:
    path = ROOT / "webapp" / "data" / "contacts.json"
    if not path.exists():
        raise SystemExit(f"Missing {path}; run the contact build first.")
    return json.loads(path.read_text(encoding="utf-8"))


def load_current_firms() -> dict[str, set[str]]:
    path = ROOT / "data" / "interim" / "advisor_employments.parquet"
    frame = pd.read_parquet(path, columns=["advisor_crd", "firm_crd"]).fillna("")
    out: dict[str, set[str]] = {}
    for row in frame.to_dict("records"):
        crd, firm = normalize_crd(row["advisor_crd"]), normalize_crd(row["firm_crd"])
        if crd and firm:
            out.setdefault(crd, set()).add(firm)
    return out


def bounded_match_score(value) -> float | None:
    """Return a finite matcher score in the producer's real range."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score) or not 0 <= score <= MATCH_SCORE_MAX:
        return None
    return round(score, 3)


def registry_quality_summary(payload: dict) -> dict:
    """PII-free release aggregates; counts are coverage, never accuracy.

    Source-level precision needs labelled source-level truth. These aggregates
    expose the size and score-evidence coverage of every population so release
    review can see where that truth is missing without pretending the aggregate
    calibration transfers equally to every roster.
    """
    recipients = payload.get("recipients") or {}
    ineligible = payload.get("ineligible") or {}
    tiers = Counter()
    sources = Counter()
    tier_sources = Counter()
    score_coverage = Counter()
    for row in recipients.values():
        tier = clean_text(row.get("tier")).lower() or "unknown"
        source = clean_text(row.get("source")) or "unknown"
        tiers[tier] += 1
        sources[source] += 1
        tier_sources[(tier, source)] += 1
        if bounded_match_score(row.get("matchScore")) is not None:
            score_coverage[(tier, source)] += 1
    return {
        "recipientCount": len(recipients),
        "ineligibleCount": len(ineligible),
        "tiers": dict(sorted(tiers.items())),
        "sources": dict(sorted(sources.items())),
        "tierSources": [
            {"tier": tier, "source": source, "recipients": count,
             "withMatchScore": score_coverage[(tier, source)]}
            for (tier, source), count in sorted(tier_sources.items())
        ],
        "ineligibleReasons": dict(sorted(Counter(ineligible.values()).items())),
    }


def team_hints(payload: dict) -> tuple[dict, dict]:
    """Return CRD -> group key and both kinds of contact-group membership.

    Team/practice membership is only a relationship hint. Identity, email and
    eligibility always come from the registry recipient assembled below.
    """
    advisors = payload.get("advisors", {})
    groups = {**(payload.get("teams", {}) or {}),
              **(payload.get("practices", {}) or {})}
    membership = {}
    for crd, row in advisors.items():
        if not isinstance(row, dict):
            continue
        key = clean_text(row.get("pk") or row.get("tm"))
        if key:
            membership[normalize_crd(crd)] = key
    return membership, groups


def _contacts_from_links(links: pd.DataFrame) -> dict:
    """Small fixture/default for callers that test the pure registry builder."""
    advisors = {}
    for row in links.fillna("").to_dict("records"):
        crd = normalize_crd(row.get("advisor_crd"))
        if not crd:
            continue
        advisors[crd] = {
            "e": normalize_email(row.get("email")),
            "n": clean_text(row.get("display_name") or row.get("legal_name")),
            "sal": clean_text(row.get("email_greeting")),
            "ln": clean_text(row.get("act_last_name")),
            "cn": clean_text(row.get("firm")),
            "src": "CRM", "t": "confirmed", "ms": 1.0,
        }
    return {"advisors": advisors, "teams": {}, "practices": {}}


def build_registry(links: pd.DataFrame, contacts_payload: dict | None = None,
                   provenance: dict | None = None,
                   current_firms: dict[str, set[str]] | None = None) -> dict:
    """Build every business-authorized route; the ledger gates Act routes.

    ``confirmed`` and ``high`` are authorized for the controlled composer by
    business decision. They do not carry the same evidence: high remains a
    probabilistic name/firm/location match. ``review`` is unresolved and never
    enters this registry. A CRM row is included only when the Act GUID, CRD and
    email agree with one unique, approved identity-ledger row.
    """
    rows = links.fillna("").to_dict("records")
    approved_links = [r for r in rows if r["identity_status"] == "approved" and
                      bool(r["can_email"]) and normalize_crd(r["advisor_crd"]) and
                      normalize_email(r["email"])]
    links_by_crd, unsafe_link_crds = {}, {}
    link_crd_count = Counter(normalize_crd(r["advisor_crd"])
                             for r in approved_links)
    link_email_count = Counter(normalize_email(r["email"])
                               for r in approved_links)
    for row in approved_links:
        crd, email = normalize_crd(row["advisor_crd"]), normalize_email(row["email"])
        if link_crd_count[crd] != 1:
            unsafe_link_crds[crd] = "approved_crd_not_unique"
        elif link_email_count[email] != 1:
            unsafe_link_crds[crd] = "approved_email_not_unique"
        else:
            links_by_crd[crd] = row

    contacts_payload = contacts_payload or _contacts_from_links(links)
    contacts = contacts_payload.get("advisors", {}) or {}
    recipients, ineligible = {}, {}
    for raw_crd, contact in contacts.items():
        crd = normalize_crd(raw_crd)
        if not crd or not isinstance(contact, dict):
            continue
        email = normalize_email(contact.get("e"))
        tier = clean_text(contact.get("t")).lower()
        source = clean_text(contact.get("src"))
        if tier not in EXPORTED_TIERS:
            ineligible[crd] = "contact_identity_not_approved"
            continue
        if not email:
            ineligible[crd] = "missing_or_invalid_email"
            continue
        match_score = bounded_match_score(contact.get("ms"))
        if tier == "high" and (not source or match_score is None):
            ineligible[crd] = "high_match_evidence_missing"
            continue
        if current_firms is not None and source.upper() not in {"CRM", "EIC"}:
            allowed = {normalize_crd(value) for value in contact.get("rf", [])
                       if normalize_crd(value)}
            if not allowed:
                ineligible[crd] = "roster_firm_evidence_missing"
                continue
            if not allowed & current_firms.get(crd, set()):
                ineligible[crd] = "roster_current_firm_conflict"
                continue
        # Internal colleagues are EXPORTED, flagged, and refused at run time
        # unless the address or domain is on EMAIL_INTERNAL_RECIPIENT_ALLOWLIST.
        #
        # Excluding them here instead removed the only safe way to test the
        # emailer -- every rehearsal batch is addressed to this firm -- and it
        # did so silently, blocking the account that does the testing. Deciding
        # it at run time keeps them out of advisor campaigns while letting an
        # administrator name the addresses a rehearsal may use, without a
        # rebuild and re-upload of the registry.
        internal = source.upper() == "EIC" or email.endswith("@eicatlanta.com")
        approved = links_by_crd.get(crd)
        if source.upper() == "CRM":
            if not approved:
                ineligible[crd] = unsafe_link_crds.get(
                    crd, "act_identity_not_approved")
                continue
            if normalize_email(approved.get("email")) != email:
                ineligible[crd] = "act_email_does_not_match_approved_identity"
                continue
        # A roster winner may also have an exact approved Act row. Attach its
        # GUID only when its email agrees, so later CRM writes remain exact.
        exact_act = approved if approved and normalize_email(
            approved.get("email")) == email else None
        name = clean_text(contact.get("pn") or contact.get("n") or
                          (exact_act or {}).get("display_name"))
        greeting = clean_text(contact.get("sal") or
                              (exact_act or {}).get("email_greeting"))
        # A display-name tail may be a suffix or credential (Lynn Shaw II,
        # Brad Dickens, C(k)P).  The contact build carries the resolved SEC
        # surname explicitly; never reinterpret presentation text as identity.
        last_name = clean_text(contact.get("ln")
                               or (exact_act or {}).get("act_last_name"))
        # Names are outbound merge data, not cosmetic metadata. If neither an
        # approved ACT greeting nor the post-identity roster resolver can
        # produce both fields, keep the person visible/callable but fail the
        # controlled email route closed instead of splitting a dirty display
        # name at send time.
        last_token = re.sub(r"[^a-z]", "", last_name.casefold())
        unsafe_last = last_token in {
            "jr", "sr", "ii", "iii", "iv", "v", "vi", "esq",
            "cfa", "cfp", "cpa", "cima", "chfc", "clu",
            "mba", "phd", "ricp", "crpc", "cpwa", "planner",
        }
        if not valid_greeting(greeting) or not last_name or unsafe_last:
            ineligible[crd] = "contact_presentation_not_approved"
            continue
        recipient = {
            "eligible": True, "email": email,
            "name": name, "greetingName": greeting,
            "lastName": last_name,
            "firm": clean_text(contact.get("cn") or
                               (exact_act or {}).get("firm")),
            "tier": tier, "source": source, "internal": internal,
            "actContactId": clean_text(
                (exact_act or {}).get("source_record_id")),
            "teammates": [],
        }
        if clean_text(contact.get("ps")):
            recipient["greetingSource"] = clean_text(contact.get("ps"))
        if clean_text(contact.get("pih")):
            recipient["greetingEvidenceHash"] = clean_text(contact.get("pih"))
        if clean_text(contact.get("im")):
            recipient["identityMethod"] = clean_text(contact.get("im"))
            recipient["identityAnchors"] = sorted({
                normalize_crd(value) for value in contact.get("xa", [])
                if normalize_crd(value)})
        if match_score is not None:
            recipient["matchScore"] = match_score
        recipients[crd] = recipient

    # Enforce one CRD and one address globally after Act and roster routes merge.
    email_count = Counter(row["email"] for row in recipients.values())
    for crd in list(recipients):
        if email_count[recipients[crd]["email"]] > 1:
            recipients.pop(crd)
            ineligible[crd] = "approved_email_not_unique"

    advisor_team, groups = team_hints(contacts_payload)
    for crd, recipient in recipients.items():
        key = advisor_team.get(crd, "")
        members = (groups.get(key, {}) or {}).get("m", []) if key else []
        pairs = sorted({(normalize_crd(item[0]), recipients[normalize_crd(item[0])]["email"])
                        for item in members if isinstance(item, list) and item and
                        normalize_crd(item[0]) in recipients and
                        normalize_crd(item[0]) != crd})
        recipient["teammates"] = [
            {"crd": mate_crd, "email": email,
             "name": recipients[mate_crd]["name"]}
            for mate_crd, email in pairs]
    for crd, recipient in recipients.items():
        route = {
            "crd": crd, "email": recipient["email"],
            "actContactId": recipient["actContactId"],
            "teammates": sorted([[m["crd"], m["email"]]
                                 for m in recipient["teammates"]]),
        }
        recipient["routingHash"] = content_hash(route)
        ineligible.pop(crd, None)
    core = {"schemaVersion": 1, "recipients": dict(sorted(recipients.items())),
            "ineligible": dict(sorted(ineligible.items())),
            "provenance": dict(sorted((provenance or {}).items()))}
    return {**core, "contentHash": runtime_content_hash(core),
            "generated": dt.datetime.now(dt.timezone.utc).isoformat()}


def write_registry(payload: dict) -> tuple[pathlib.Path, pathlib.Path]:
    json_path = IDENTITY / APPROVED_REGISTRY_FILENAME
    gzip_path = IDENTITY / APPROVED_REGISTRY_GZIP_FILENAME
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    tmp = json_path.with_suffix(".json.tmp")
    tmp.write_bytes(raw)
    tmp.replace(json_path)
    with gzip.GzipFile(filename="", mode="wb", fileobj=gzip_path.open("wb"),
                       mtime=0) as handle:
        handle.write(raw)
    return json_path, gzip_path


def shard_key(crd: str) -> str:
    """Stable bounded shard for one numeric CRD."""
    value = normalize_crd(crd)
    return value[:SHARD_WIDTH].ljust(SHARD_WIDTH, "0")


def build_shards(payload: dict) -> tuple[dict, dict[str, dict]]:
    """Build release-bound point-lookup shards and their PII-free manifest."""
    release_prefix = f"{SHARD_PREFIX}/releases/{payload['contentHash']}"
    grouped: dict[str, dict] = {}
    for field in ("recipients", "ineligible"):
        for crd, row in (payload.get(field) or {}).items():
            key = shard_key(crd)
            grouped.setdefault(key, {"recipients": {}, "ineligible": {}})
            grouped[key][field][str(crd)] = row
    shards = {}
    entries = {}
    for key, rows in sorted(grouped.items()):
        core = {
            "schemaVersion": 1,
            "registryContentHash": payload["contentHash"],
            "shardKey": key,
            "recipients": dict(sorted(rows["recipients"].items())),
            "ineligible": dict(sorted(rows["ineligible"].items())),
        }
        # Shards are verified after JSON.parse in Node. Preserve the registry's
        # existing cross-language rule: 1.0 and 1 have the same runtime value.
        shard = {**core, "contentHash": runtime_content_hash(core)}
        blob = f"{release_prefix}/shards/{key}.json.gz"
        shards[key] = shard
        entries[key] = {
            "blob": blob,
            "contentHash": shard["contentHash"],
            "recipientCount": len(core["recipients"]),
            "ineligibleCount": len(core["ineligible"]),
        }
    manifest_core = {
        "schemaVersion": 1,
        "registrySchemaVersion": int(payload.get("schemaVersion") or 0),
        "registryContentHash": payload["contentHash"],
        "recipientCount": len(payload.get("recipients") or {}),
        "ineligibleCount": len(payload.get("ineligible") or {}),
        "provenance": payload.get("provenance") or {},
        "shards": entries,
    }
    return {**manifest_core, "contentHash": content_hash(manifest_core)}, shards


def write_shards(payload: dict) -> tuple[pathlib.Path, list[pathlib.Path]]:
    manifest, shards = build_shards(payload)
    SHARD_DIR.mkdir(parents=True, exist_ok=True)
    expected = set()
    paths = []
    for key, shard in shards.items():
        path = SHARD_DIR / f"{key}.json.gz"
        raw = json.dumps(shard, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        with gzip.GzipFile(filename="", mode="wb", fileobj=path.open("wb"),
                           mtime=0) as handle:
            handle.write(raw)
        expected.add(path.name)
        paths.append(path)
    for stale in SHARD_DIR.glob("*.json.gz"):
        if stale.name not in expected:
            stale.unlink()
    raw_manifest = (json.dumps(manifest, ensure_ascii=False,
                               sort_keys=True, indent=2) + "\n").encode("utf-8")
    tmp = SHARD_MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_bytes(raw_manifest)
    tmp.replace(SHARD_MANIFEST_PATH)
    return SHARD_MANIFEST_PATH, paths


def build_release_descriptor(payload: dict, shard_manifest: dict | None = None) -> dict:
    """Pin this registry build into the API without copying recipient PII."""
    provenance = payload.get("provenance") or {}
    unexpected = sorted(set(provenance) - set(RELEASE_PROVENANCE_KEYS))
    if unexpected:
        raise ValueError("Registry provenance contains unapproved fields: " +
                         ", ".join(unexpected))
    missing = [key for key in RELEASE_PROVENANCE_KEYS
               if not clean_text(provenance.get(key))]
    if missing:
        raise ValueError("Registry provenance is missing: " + ", ".join(missing))
    registry_hash = clean_text(payload.get("contentHash")).lower()
    if len(registry_hash) != 64 or any(c not in "0123456789abcdef"
                                       for c in registry_hash):
        raise ValueError("Registry contentHash is invalid")
    core = {
        "schemaVersion": 1,
        "registrySchemaVersion": int(payload.get("schemaVersion") or 0),
        "registryContentHash": registry_hash,
        "recipientCount": len(payload.get("recipients") or {}),
        "ineligibleCount": len(payload.get("ineligible") or {}),
        "provenance": {key: provenance[key]
                       for key in sorted(RELEASE_PROVENANCE_KEYS)},
        "shardManifestHash": clean_text(
            (shard_manifest or {}).get("contentHash")).lower(),
    }
    if len(core["shardManifestHash"]) != 64:
        raise ValueError("Shard manifest contentHash is invalid")
    return {**core, "descriptorHash": content_hash(core)}


def write_release_descriptor(payload: dict, shard_manifest: dict) -> pathlib.Path:
    descriptor = build_release_descriptor(payload, shard_manifest)
    raw = (json.dumps(descriptor, ensure_ascii=False, sort_keys=True, indent=2) +
           "\n").encode("utf-8")
    tmp = RELEASE_DESCRIPTOR_PATH.with_suffix(".json.tmp")
    tmp.write_bytes(raw)
    tmp.replace(RELEASE_DESCRIPTOR_PATH)
    return RELEASE_DESCRIPTOR_PATH


def upload_shards(manifest_path: pathlib.Path,
                  shard_paths: list[pathlib.Path]) -> int:
    """Explicit only; local generation never needs the Azure SDK or credential."""
    try:
        from azure.storage.blob import BlobServiceClient, ContentSettings
    except ImportError:
        print("azure-storage-blob is not installed: pip install azure-storage-blob")
        return 1
    connection = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
    if not connection:
        print("AZURE_STORAGE_CONNECTION_STRING is not set.")
        return 1
    container = BlobServiceClient.from_connection_string(
        connection).get_container_client(BLOB_CONTAINER)
    try:
        container.create_container()
    except Exception:
        pass
    gzip_settings = ContentSettings(content_type="application/json",
                                    content_encoding="gzip")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    release_prefix = (f"{SHARD_PREFIX}/releases/"
                      f"{manifest['registryContentHash']}")
    for shard_path in shard_paths:
        entry = (manifest.get("shards") or {}).get(shard_path.stem)
        blob = clean_text((entry or {}).get("blob"))
        if not blob.startswith(f"{release_prefix}/shards/"):
            raise ValueError(f"Unsafe shard blob path for {shard_path.name}")
        with shard_path.open("rb") as handle:
            container.get_blob_client(blob).upload_blob(
                handle, overwrite=True, content_settings=gzip_settings)
    manifest_blob = f"{release_prefix}/manifest.json"
    with manifest_path.open("rb") as handle:
        container.get_blob_client(manifest_blob).upload_blob(
            handle, overwrite=True, content_settings=ContentSettings(
                content_type="application/json"))
    print(f"[+] uploaded {len(shard_paths):,} shards and "
          f"{BLOB_CONTAINER}/{manifest_blob}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--upload", action="store_true",
                        help="upload release-bound shards and manifest")
    args = parser.parse_args()
    links_path = IDENTITY / LINKS_FILENAME
    manifest_path = IDENTITY / MANIFEST_FILENAME
    contacts_path = ROOT / "webapp" / "data" / "contacts.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_core = {k: v for k, v in manifest.items()
                     if k not in {"generatedUtc", "contentHash"}}
    if manifest.get("contentHash") != content_hash(manifest_core):
        raise SystemExit("Identity manifest contentHash is invalid")

    def file_hash(path):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    expected_links = ((manifest.get("outputs") or {})
                      .get(LINKS_FILENAME, {}).get("sha256"))
    actual_links = file_hash(links_path)
    if not expected_links or expected_links != actual_links:
        raise SystemExit("Identity links do not match the identity manifest")
    links = pd.read_parquet(links_path)
    contacts_payload = load_contacts()
    contacts_provenance = contacts_payload.get("provenance") or {}
    expected_contact_provenance = {
        "identityManifestHash": manifest["contentHash"],
        "identityLinksSha256": actual_links,
        "actSource": (manifest.get("actSource") or {}).get("file", ""),
        "actSourceSha256": (manifest.get("actSource") or {}).get("sha256", ""),
    }
    if contacts_provenance != expected_contact_provenance:
        raise SystemExit(
            "contacts.json was not built from this exact identity ledger and "
            "Act snapshot; run python src/build_contacts.py")
    payload = build_registry(links, contacts_payload, {
        "identityManifestHash": manifest["contentHash"],
        "identityLinksSha256": actual_links,
        "contactsSha256": file_hash(contacts_path),
        "actSource": (manifest.get("actSource") or {}).get("file", ""),
        "actSourceSha256": (manifest.get("actSource") or {}).get("sha256", ""),
    }, load_current_firms())
    json_path, gzip_path = write_registry(payload)
    shard_manifest_path, shard_paths = write_shards(payload)
    shard_manifest = json.loads(shard_manifest_path.read_text(encoding="utf-8"))
    descriptor_path = write_release_descriptor(payload, shard_manifest)
    print(f"[+] {json_path}: {len(payload['recipients']):,} approved; "
          f"{len(payload['ineligible']):,} ineligible")
    print(f"[+] {gzip_path} ({gzip_path.stat().st_size / 1024:.1f} KiB)")
    print(f"[+] {shard_manifest_path}: {len(shard_paths):,} CRD shards")
    print(f"[+] {descriptor_path}: release-bound to {payload['contentHash']}")
    summary = registry_quality_summary(payload)
    print("[*] authorized tiers: " + ", ".join(
        f"{tier} {count:,}" for tier, count in summary["tiers"].items()))
    print("[*] authorized routes by tier and source "
          "(matchScore coverage is evidence coverage, not precision):")
    for row in summary["tierSources"]:
        print(f"    {row['tier']:<10} {row['source']:<36} "
              f"{row['recipients']:>7,} routes  "
              f"score {row['withMatchScore']:>7,}")
    if summary["ineligibleReasons"]:
        print("[*] ineligible reasons: " + ", ".join(
            f"{reason} {count:,}" for reason, count in
            summary["ineligibleReasons"].items()))
    if args.upload:
        return upload_shards(shard_manifest_path, shard_paths)
    print("[*] local only; --upload was not requested")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
