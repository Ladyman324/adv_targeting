"""Independent, manifest-bound exceptions to the legacy given-name audit."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from contact_provenance import sha256_file
from identity_schema import EVIDENCE_FILENAME, LINKS_FILENAME, MANIFEST_FILENAME, content_hash


def corroborated_name_pairs(identity: Path, sec_advisors: Path) -> set[tuple[str, str]]:
    """Recognize narrow corroborated bridges, never a global nickname alias.

    Fail closed on absent/old evidence; malformed or changed bound artifacts
    are errors, not permission to skip the independent name audit.
    """
    path = identity / MANIFEST_FILENAME
    if not path.exists():
        return set()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    core = {k: v for k, v in manifest.items() if k not in {"generatedUtc", "contentHash"}}
    if manifest.get("contentHash") != content_hash(core):
        raise ValueError("Invalid identity manifest for corroborated names")
    for name in (EVIDENCE_FILENAME, LINKS_FILENAME):
        if (manifest.get("outputs", {}).get(name, {}).get("sha256")
                != sha256_file(identity / name)):
            raise ValueError("Corroborated-name artifact is not manifest-bound: " + name)
    if manifest.get("secSources", {}).get(sec_advisors.name) != sha256_file(sec_advisors):
        raise ValueError("Corroborated names refer to different SEC individuals")
    evidence = pd.read_parquet(identity / EVIDENCE_FILENAME).fillna("")
    if "given_corroborated" not in evidence:
        return set()  # older ledgers did not support these bridges
    links = pd.read_parquet(identity / LINKS_FILENAME).fillna("")
    by_key = {}
    for row in evidence.to_dict("records"):
        key = (str(row["source_record_id"]), str(row["advisor_crd"]), row["evidence_hash"])
        if key in by_key:
            raise ValueError("Duplicate corroborated-name identity evidence")
        by_key[key] = row
    accepted = set()
    reasons = {"given_nickname_email_corroborated", "given_initial_full_middle_corroborated"}
    required = ("given_corroborated", "crd_exists", "full_surname_exact", "suffix_agrees",
                "email_valid", "email_unique_in_act", "email_personal", "firm_current_agrees",
                "street_exact", "city_exact", "state_exact", "postal_exact")
    for link in links.to_dict("records"):
        if (link["identity_status"] != "approved" or link["can_sync_act"] != True
                or link["decision_source"] != "automatic_rules"):
            continue
        key = (str(link["source_record_id"]), str(link["advisor_crd"]), link["assertion_evidence_hash"])
        row = by_key.get(key)
        if not row or not all(row.get(field) == True for field in required):
            continue
        if row.get("email_domain_current_conflicts") == True:
            continue
        if json.loads(row["hard_conflicts_json"] or "[]") or json.loads(link["hard_conflicts_json"] or "[]"):
            continue
        if not reasons.intersection(json.loads(row["positive_evidence_json"] or "[]")):
            continue
        if str(row["claimed_crd"]) != key[1] or str(link["claimed_crd"]) != key[1]:
            continue
        accepted.add((key[1], key[0]))
    return accepted
