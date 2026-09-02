"""Pure rules for ACT-only economic (asset) identity links.

Economic links may display ACT assets on an SEC pin. They never authorize
email, calls, preferred names, ACT synchronization, or CRD write-back.
"""
from __future__ import annotations

import collections
import json
from typing import Iterable, Mapping

from identity_normalize import (clean_text, given_agreement, is_generic_email,
                                name_token, normalize_crd, normalize_email,
                                normalize_suffix)

RULESET_VERSION = "act-economic-v1.0"
SCHEMA_VERSION = 1
WINNER_MARGIN = 0.12
APPROVED_LINK_TYPES = {"identity_approved", "roster_exact", "residual_strict"}

LINK_COLUMNS = [
    "act_id", "advisor_crd", "economic_status", "link_type", "reason",
    "candidate_crd", "candidate_score", "candidate_gap",
    "account_codes_json", "account_count", "relationship_value",
    "act_name", "act_company", "act_city", "act_state", "act_email",
    "current_firm_crds_json", "allowed_firm_crds_json",
    "evidence_json", "identity_evidence_hash", "ruleset_version",
    "can_email", "can_call", "can_sync_act", "can_supply_name",
]


def personal_email(value: object) -> str:
    email = normalize_email(value)
    return "" if not email or is_generic_email(email) else email


def unique_validated_roster_email(rows: Iterable[Mapping]) -> dict[str, dict]:
    """Return personal emails with exactly one wholly validated occurrence.

    Group before filtering. A high row plus an unresolved/review row is not
    unique evidence. Byte-equivalent duplicate source rows are collapsed
    because repeating the same published fact is not a competing occurrence.
    """
    by_email = collections.defaultdict(list)
    seen = collections.defaultdict(set)
    for row in rows:
        email = personal_email(row.get("email"))
        if not email:
            continue
        item = dict(row)
        fingerprint = tuple(clean_text(item.get(key)) for key in (
            "advisor_crd", "tier", "source_slug", "source_file", "name",
            "city", "state", "allowed_firm_crds"))
        if fingerprint in seen[email]:
            continue
        seen[email].add(fingerprint)
        by_email[email].append(item)
    out = {}
    for email, group in by_email.items():
        validated = [
            row for row in group
            if normalize_crd(row.get("advisor_crd")) and
            clean_text(row.get("tier")).lower() in {"confirmed", "high"}]
        if len(group) == 1 and len(validated) == 1:
            out[email] = validated[0]
    return out


def sec_name_agreement(record: Mapping, sec: Mapping,
                       aliases: Iterable[Mapping],
                       require_suffix_presence_match: bool = False
                       ) -> tuple[bool, str]:
    """Strict surname/suffix/given-name agreement over all SEC-filed names."""
    act_last = name_token(record.get("norm_last_name") or
                          record.get("raw_last_name"))
    act_suffix = normalize_suffix(record.get("raw_suffix"))
    names = [dict(sec or {}), *(dict(x) for x in aliases)]
    same_last = [x for x in names if name_token(x.get("last_name")) == act_last]
    if not act_last or not same_last:
        return False, "surname_conflict"
    for item in same_last:
        sec_suffix = normalize_suffix(item.get("suffix") or sec.get("suffix"))
        if require_suffix_presence_match and bool(act_suffix) != bool(sec_suffix):
            continue
        if act_suffix and sec_suffix and act_suffix != sec_suffix:
            continue
        agrees, reason = given_agreement(
            [record.get("norm_first_name"), record.get("raw_first_name")],
            [item.get("first_name"), item.get("middle_name"),
             item.get("used_first_name"), sec.get("first_name"),
             sec.get("middle_name"), sec.get("used_first_name")])
        if agrees:
            return True, reason
    return False, "given_or_suffix_conflict"


def firm_agreement(record: Mapping, current: Mapping,
                   allowed_firm_crds: Iterable[str]) -> tuple[bool, str]:
    current_crds = {normalize_crd(x) for x in current.get("firm_crds", set())
                    if normalize_crd(x)}
    allowed = {normalize_crd(x) for x in allowed_firm_crds if normalize_crd(x)}
    if allowed:
        if current_crds & allowed:
            return True, "approved_firm_family"
        return False, "authoritative_firm_family_mismatch"
    company = name_token(record.get("raw_company"))
    current_names = {name_token(x) for x in current.get("firm_names", set())
                     if name_token(x)}
    if company and company in current_names:
        return True, "act_company_current_firm_exact"
    return False, "current_firm_not_corroborated"


def paired_location(record: Mapping, branches: Iterable[Mapping]) -> tuple[bool, int]:
    """Require city and state on the same SEC branch; return evidence strength."""
    city = name_token(record.get("raw_city"))
    state = clean_text(record.get("raw_state")).upper()
    postal = clean_text(record.get("raw_postal"))[:5]
    street = name_token(record.get("raw_street"))
    best = 0
    for branch in branches:
        if (city and state and name_token(branch.get("city")) == city and
                clean_text(branch.get("state")).upper() == state):
            strength = 1
            if postal and clean_text(branch.get("postal"))[:5] == postal:
                strength += 1
            if street and name_token(branch.get("street")) == street:
                strength += 1
            best = max(best, strength)
    return bool(best), best


def choose_with_margin(scored: Iterable[tuple[float, str]],
                       margin: float = WINNER_MARGIN) -> tuple[str, float, float, str]:
    ranked = sorted(((float(score), normalize_crd(crd)) for score, crd in scored
                     if normalize_crd(crd)), reverse=True)
    if not ranked:
        return "", 0.0, 0.0, "no_strict_candidate"
    best_score, best_crd = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    gap = best_score - second
    if len(ranked) > 1 and gap < margin:
        return best_crd, best_score, gap, "ambiguous_runner_up"
    return best_crd, best_score, gap, "winner"


def enforce_global_one_to_one(proposals, already_claimed=()):
    """Reject collisions globally; never choose an arbitrary ACT row."""
    claimed = {normalize_crd(x) for x in already_claimed if normalize_crd(x)}
    by_crd = collections.defaultdict(list)
    for act_id, (crd, _score, _gap, verdict) in proposals.items():
        if crd and verdict == "winner" and crd not in claimed:
            by_crd[crd].append(act_id)
    out = dict(proposals)
    for crd, act_ids in by_crd.items():
        if len(act_ids) > 1:
            for act_id in act_ids:
                _, score, gap, _ = out[act_id]
                out[act_id] = (crd, score, gap, "global_crd_collision")
    return out


def account_bucket(link_types: Iterable[str], statuses: Iterable[str]) -> str:
    approved = set(link_types)
    for kind in ("identity_approved", "roster_exact", "residual_strict"):
        if kind in approved:
            return kind
    return "review" if "review" in set(statuses) else "unmatched"


def safety_fields() -> dict:
    return {"can_email": False, "can_call": False, "can_sync_act": False,
            "can_supply_name": False}


def approved_link_map(rows: Iterable[Mapping]) -> dict[str, tuple[str, str]]:
    """Return approved ACT->CRD links and fail if economic data gains authority."""
    out = {}
    crd_owner = {}
    seen_act_ids = set()
    for row in rows:
        act_id = clean_text(row.get("act_id"))
        if not act_id:
            raise ValueError("blank ACT id in economic ledger")
        if act_id in seen_act_ids:
            raise ValueError("duplicate ACT id in economic ledger")
        seen_act_ids.add(act_id)
        if any(bool(row.get(key)) for key in (
                "can_email", "can_call", "can_sync_act", "can_supply_name")):
            raise ValueError("economic link illegally carries contact authority")
        if clean_text(row.get("economic_status")) != "approved":
            continue
        crd = normalize_crd(row.get("advisor_crd"))
        link_type = clean_text(row.get("link_type"))
        if link_type not in APPROVED_LINK_TYPES:
            raise ValueError("unrecognized approved economic link type")
        if not crd:
            raise ValueError("approved economic link has no CRD")
        if act_id in out:
            raise ValueError("duplicate approved economic ACT id")
        if crd in crd_owner and crd_owner[crd] != act_id:
            raise ValueError("duplicate approved economic CRD")
        tier = ("confirmed" if link_type == "identity_approved"
                else "high")
        out[act_id] = (crd, tier)
        crd_owner[crd] = act_id
    return out


def json_list(values: Iterable[object]) -> str:
    return json.dumps(sorted({clean_text(x) for x in values if clean_text(x)}),
                      separators=(",", ":"))


def spreadsheet_safe(value: object) -> object:
    """Prevent spreadsheet formula execution in human-facing report copies."""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value
