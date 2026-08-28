"""Versioned schemas for the independent identity-evidence pipeline."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

RULESET_VERSION = "identity-v1.2"
REGISTRY_SCHEMA_VERSION = "1.0"
IDENTITY_STATUSES = frozenset({"approved", "review", "quarantine", "unmatched", "rejected"})
LINK_DECISIONS = frozenset({"approve", "reject", "replace"})
PREFERRED_DECISIONS = frozenset({"approve", "reject", ""})
ACT_RECORD_ACTIONS = frozenset({"keep_crd", "clear_crd", "set_crd", "merge_contact", "fix_name", "review_only", ""})

SOURCE_RECORD_COLUMNS = [
    "record_key", "source_system", "source_kind", "source_record_id", "source_file",
    "source_sha256", "source_edited", "source_row_number", "raw_claimed_crd",
    "raw_full_name", "raw_first_name", "raw_middle_name", "raw_last_name",
    "raw_prefix", "raw_suffix", "raw_salutation", "raw_email",
    "raw_alt_emails_json", "raw_business_phone", "raw_alt_phones_json",
    "raw_company", "raw_job_title", "raw_city", "raw_state", "raw_postal",
    "raw_street", "raw_tier", "norm_full_name", "norm_first_name",
    "norm_last_name", "norm_email", "email_domain", "norm_phone",
    "structured_name_malformed", "generic_email", "email_claim_count",
    "act_id_claim_count", "crd_claim_count", "record_active",
]

EVIDENCE_COLUMNS = [
    "record_key", "source_record_id", "advisor_crd", "claimed_crd",
    "candidate_origin", "candidate_crd", "candidate_tier", "candidate_score",
    "candidate_gap",
    "crd_exists", "full_surname_exact",
    "structured_surname_exact", "given_exact", "given_sec_used",
    "given_strict_nickname", "suffix_agrees", "act_given_name",
    "sec_first_name", "sec_middle_name", "sec_used_first_name", "sec_last_name",
    "sec_suffix", "email_valid", "email_unique_in_act", "email_personal",
    "email_surname_exact", "email_given_exact", "current_firms_json",
    "prior_firms_json", "firm_current_agrees", "firm_prior_agrees",
    "sec_branches_json", "street_exact", "city_exact", "state_exact",
    "postal_exact", "roster_evidence_json", "roster_email_exact",
    "roster_phone_exact", "roster_name_agrees", "roster_firm_current_agrees",
    "positive_evidence_json", "hard_conflicts_json", "warnings_json",
    "evidence_hash", "ruleset_version",
]

LINK_COLUMNS = [
    "record_key", "source_system", "source_record_id", "claimed_crd",
    "advisor_crd", "identity_status", "confidence_class", "decision_reason",
    "hard_conflicts_json", "warnings_json", "can_display_contact", "can_call",
    "can_email", "can_sync_act", "legal_name", "sec_used_name", "act_name",
    "act_first_name", "act_last_name", "email", "firm", "tier",
    "preferred_first", "preferred_status", "preferred_reason", "display_name",
    "email_greeting", "assertion_evidence_hash", "resolved_evidence_hash",
    "ruleset_version", "decision_source", "reviewed_by", "reviewed_utc",
]

DECISION_COLUMNS = [
    "act_id", "expected_evidence_hash", "link_decision", "resolved_crd",
    "canonical_for_crd", "act_record_action", "preferred_decision",
    "preferred_first", "reviewer", "reviewed_utc", "reason_code", "notes",
]
REGISTRY_RECIPIENT_FIELDS = ["email", "name", "greetingName", "lastName", "firm", "tier", "source", "actContactId", "teammates"]

# Files below data/identity are ignored build/review artifacts. Keeping their
# names here prevents the report, importer, ledger and registry from drifting.
IDENTITY_DIRNAME = "identity"
SOURCE_RECORDS_FILENAME = "act_source_records.parquet"
EVIDENCE_FILENAME = "act_identity_evidence.parquet"
LINKS_FILENAME = "act_identity_links.parquet"
MANIFEST_FILENAME = "identity_manifest.json"
DECISIONS_FILENAME = "act_identity_decisions.json"
CORRECTIONS_CSV_FILENAME = "act_crd_corrections.csv"
CORRECTIONS_XLSX_FILENAME = "act_crd_corrections.xlsx"
APPROVED_REGISTRY_FILENAME = "approved_recipients.json"
APPROVED_REGISTRY_GZIP_FILENAME = "approved_recipients.json.gz"


def canonical_json(value: Any) -> str:
    """Return the byte-stable JSON representation used by every content hash."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def runtime_content_hash(value: Any) -> str:
    """Hash JSON as the JavaScript verifier sees parsed numeric values.

    JSON.parse cannot retain the lexical distinction between 1 and 1.0.
    Normalize integral floats before hashing so Python and JavaScript agree.
    Registry scores are bounded decimal values, so no other number-format
    differences are possible in this payload.
    """
    def normalize(item):
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, dict):
            return {key: normalize(val) for key, val in item.items()}
        if isinstance(item, list):
            return [normalize(val) for val in item]
        return item
    return content_hash(normalize(value))


def evidence_hash(payload: Mapping[str, Any]) -> str:
    """Hash evidence facts, never build time or output row order."""
    return content_hash({"rulesetVersion": RULESET_VERSION, "evidence": dict(payload)})
