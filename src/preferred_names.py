"""Safe ACT preferred-name overlays for independently resolved roster contacts.

Identity and presentation are deliberately separate. An unmatched ACT record
cannot authorize an email route or an ACT write. It may supply the name a
person uses when an independently approved roster route agrees on the exact
primary email, current SEC firm and CRD.

The returned overlay contains no ACT GUID. It is presentation evidence only.
"""
from __future__ import annotations

import json
import pathlib
from collections import Counter

import pandas as pd

from contact_provenance import sha256_file
from identity_normalize import (clean_text, is_generic_email, name_token,
                                normalize_crd, normalize_email,
                                strict_nickname_pair)
from identity_schema import (EVIDENCE_FILENAME, LINKS_FILENAME,
                             MANIFEST_FILENAME, SOURCE_RECORDS_FILENAME,
                             content_hash)

OVERLAY_SOURCE = "act_primary_email_overlay_v1"


def _truth(value) -> bool:
    if isinstance(value, bool):
        return value
    return clean_text(value).lower() in {"1", "true", "yes"}


def _empty_json_list(value) -> bool:
    try:
        parsed = json.loads(clean_text(value) or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(parsed, list) and not parsed


def _preferred_token(value) -> str:
    text = clean_text(value)
    if (not text or len(text) > 40 or any(ch.isspace() for ch in text)
            or not text[0].isalpha()
            or any(not (ch.isalpha() or ch in "-'’") for ch in text)):
        return ""
    return text


def preferred_display_name(formal_name: object, preferred_first: object) -> str:
    """Show a preference transparently, for example Christopher (Chris) Tolman."""
    formal = clean_text(formal_name)
    preferred = _preferred_token(preferred_first)
    if not formal or not preferred:
        return formal
    parts = formal.split()
    if name_token(parts[0]) == name_token(preferred):
        return formal
    # If the view already uses a strict nickname and ACT supplies the legal
    # form, adding it in parentheses is backwards: Chris (Christopher).
    if strict_nickname_pair(preferred, parts[0]):
        return formal
    if len(parts) > 1 and parts[1].startswith("(") and parts[1].endswith(")"):
        return formal
    return f"{parts[0]} ({preferred})" + (
        f" {' '.join(parts[1:])}" if len(parts) > 1 else "")


def build_greeting_overlays(source: pd.DataFrame, evidence: pd.DataFrame,
                            links: pd.DataFrame) -> dict[str, dict[str, str]]:
    """Return CRD-keyed presentation overlays from already verified inputs."""
    required_source = {
        "record_key", "source_system", "raw_claimed_crd", "raw_salutation", "norm_email",
        "generic_email", "email_claim_count", "record_active",
    }
    required_evidence = {
        "record_key", "candidate_crd", "candidate_tier", "candidate_score",
        "candidate_gap", "full_surname_exact", "given_exact",
        "given_sec_used", "given_strict_nickname", "sec_first_name",
        "sec_used_first_name", "email_unique_in_act", "email_personal",
        "email_domain_current_agrees", "firm_current_agrees",
        "roster_email_exact", "roster_name_agrees",
        "roster_firm_current_agrees", "hard_conflicts_json", "evidence_hash",
    }
    required_links = {
        "record_key", "identity_status", "decision_reason",
        "preferred_first", "preferred_status", "preferred_reason",
        "resolved_evidence_hash",
    }
    for label, frame, required in (
            ("source", source, required_source),
            ("evidence", evidence, required_evidence),
            ("links", links, required_links)):
        missing = sorted(required - set(frame.columns))
        if missing:
            raise RuntimeError(
                f"Identity {label} artifact is missing: {', '.join(missing)}")
        if frame["record_key"].astype(str).duplicated().any():
            raise RuntimeError(f"Identity {label} artifact has duplicate record keys.")

    evidence_by_key = {str(row["record_key"]): row
                       for row in evidence.fillna("").to_dict("records")}
    links_by_key = {str(row["record_key"]): row
                    for row in links.fillna("").to_dict("records")}
    candidates = []
    for src in source.fillna("").to_dict("records"):
        key = str(src["record_key"])
        ev, link = evidence_by_key.get(key), links_by_key.get(key)
        if not ev or not link:
            continue
        email = normalize_email(src["norm_email"])
        crd = normalize_crd(ev["candidate_crd"])
        greeting = _preferred_token(link["preferred_first"])
        try:
            score, gap = float(ev["candidate_score"]), float(ev["candidate_gap"])
            claim_count = int(float(src["email_claim_count"]))
        except (TypeError, ValueError):
            continue
        preferred_t = name_token(greeting)
        sec_legal_t = name_token(ev["sec_first_name"])
        sec_used_t = name_token(ev["sec_used_first_name"])
        nickname_ok = link["preferred_reason"] == "preferred_strict_nickname"
        sec_used_ok = (link["preferred_reason"] == "preferred_exact"
                       and preferred_t and preferred_t == sec_used_t
                       and preferred_t != sec_legal_t)
        row_hash = clean_text(ev["evidence_hash"])
        safe = (
            crd and email and greeting
            and clean_text(src["source_system"]).lower() == "act"
            and name_token(src["raw_salutation"]) == name_token(greeting)
            and not normalize_crd(src["raw_claimed_crd"])
            and _truth(src["record_active"])
            and not _truth(src["generic_email"])
            and not is_generic_email(email)
            and claim_count == 1
            and _truth(ev["email_unique_in_act"])
            and _truth(ev["email_personal"])
            and link["identity_status"] == "unmatched"
            and link["decision_reason"] == "no_asserted_crd"
            and ev["candidate_tier"] == "high"
            and abs(score - 1.0) < 1e-9 and gap >= 0.25
            and _truth(ev["full_surname_exact"])
            and (_truth(ev["given_exact"]) or _truth(ev["given_sec_used"])
                 or _truth(ev["given_strict_nickname"]))
            and _truth(ev["roster_email_exact"])
            and _truth(ev["roster_name_agrees"])
            and _truth(ev["roster_firm_current_agrees"])
            and _truth(ev["firm_current_agrees"])
            and _truth(ev["email_domain_current_agrees"])
            and _empty_json_list(ev["hard_conflicts_json"])
            and link["preferred_status"] == "approved_auto"
            and (nickname_ok or sec_used_ok)
            and row_hash
            and clean_text(link["resolved_evidence_hash"]) == row_hash
        )
        if safe:
            candidates.append({
                "crd": crd, "email": email, "greeting": greeting,
                "evidenceHash": row_hash, "source": OVERLAY_SOURCE,
            })

    # A presentation hint must be one-to-one too. Reject every side of any
    # collision instead of selecting whichever parquet row happens to be first.
    crd_count = Counter(row["crd"] for row in candidates)
    email_count = Counter(row["email"] for row in candidates)
    return {row["crd"]: {k: v for k, v in row.items() if k != "crd"}
            for row in candidates
            if crd_count[row["crd"]] == 1 and email_count[row["email"]] == 1}


def load_greeting_overlays(identity_dir: pathlib.Path,
                           contacts_path: pathlib.Path) -> dict[str, dict[str, str]]:
    """Validate the manifest and load overlays for these exact ACT bytes."""
    identity_dir = pathlib.Path(identity_dir)
    contacts_path = pathlib.Path(contacts_path)
    manifest_path = identity_dir / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    core = {k: v for k, v in manifest.items()
            if k not in {"generatedUtc", "contentHash"}}
    if manifest.get("contentHash") != content_hash(core):
        raise RuntimeError("Identity manifest contentHash is invalid.")
    act = manifest.get("actSource") or {}
    if (act.get("file") != contacts_path.name
            or act.get("sha256") != sha256_file(contacts_path)):
        raise RuntimeError("Preferred names describe different ACT contact bytes.")

    frames = {}
    for filename in (SOURCE_RECORDS_FILENAME, EVIDENCE_FILENAME, LINKS_FILENAME):
        path = identity_dir / filename
        meta = (manifest.get("outputs") or {}).get(filename) or {}
        if (not path.exists() or meta.get("sha256") != sha256_file(path)
                or int(meta.get("rows") or -1) != int(act.get("rows") or -2)):
            raise RuntimeError(f"Identity artifact is stale or modified: {filename}")
        frame = pd.read_parquet(path).fillna("")
        if len(frame) != int(meta["rows"]):
            raise RuntimeError(f"Identity artifact row count changed: {filename}")
        frames[filename] = frame
    return build_greeting_overlays(
        frames[SOURCE_RECORDS_FILENAME], frames[EVIDENCE_FILENAME],
        frames[LINKS_FILENAME])
