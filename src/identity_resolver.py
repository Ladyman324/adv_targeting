"""Pure, fail-closed rules for resolving Act JSON CRD assertions to SEC people."""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Iterable, Mapping, Optional

from identity_normalize import (
    clean_text, given_agreement, is_generic_email, name_token, normalize_crd, normalize_email,
    normalize_phone, normalize_suffix, parse_full_name, preferred_name_status,
    structured_name,
)
from identity_schema import RULESET_VERSION, evidence_hash


def _json(values: Iterable[str]) -> str:
    return json.dumps(sorted(set(x for x in values if x)), separators=(",", ":"))


def sec_name(sec: Mapping[str, Any]) -> str:
    return " ".join(clean_text(sec.get(k)) for k in
                    ("first_name", "middle_name", "last_name", "suffix")
                    if clean_text(sec.get(k)))


def prepare_act_records(rows: list[dict], source_file: str,
                        source_sha256: str) -> list[dict]:
    """Normalize Act API rows only; no Excel column names exist in this path."""
    ids = Counter(clean_text(r.get("id")) for r in rows if clean_text(r.get("id")))
    crds = Counter(normalize_crd((r.get("customFields") or {}).get("sec_crd") or
                                 (r.get("customFields") or {}).get("crd"))
                   for r in rows)
    emails = Counter(normalize_email(r.get("emailAddress")) for r in rows)
    out = []
    for row_number, raw in enumerate(rows, 1):
        custom, address = raw.get("customFields") or {}, raw.get("businessAddress") or {}
        act_id = clean_text(raw.get("id"))
        raw_crd = clean_text(custom.get("sec_crd") or custom.get("crd"))
        crd, email = normalize_crd(raw_crd), normalize_email(raw.get("emailAddress"))
        full = parse_full_name(raw.get("fullName"))
        structured = structured_name(raw.get("firstName"), raw.get("middleName"),
                                     raw.get("lastName"), raw.get("nameSuffix"))
        canonical = full if full.last else structured
        alt_emails = [normalize_email(raw.get(k)) for k in
                      ("alternateEmailAddress", "personalEmailAddress")]
        alt_emails.append(normalize_email(custom.get("email_2_email")))
        alt_phones = [normalize_phone(raw.get(k)) for k in
                      ("mobilePhone", "homePhone", "alternatePhone")]
        out.append({
            "record_key": f"act:{act_id}" if act_id else f"act-row:{row_number}",
            "source_system": "act", "source_kind": "act_api_json",
            "source_record_id": act_id, "source_file": source_file,
            "source_sha256": source_sha256, "source_edited": clean_text(raw.get("edited")),
            "source_row_number": row_number, "raw_claimed_crd": raw_crd,
            "raw_full_name": clean_text(raw.get("fullName")),
            "raw_first_name": clean_text(raw.get("firstName")),
            "raw_middle_name": clean_text(raw.get("middleName")),
            "raw_last_name": clean_text(raw.get("lastName")),
            "raw_prefix": clean_text(raw.get("namePrefix")),
            "raw_suffix": clean_text(raw.get("nameSuffix")),
            "raw_salutation": clean_text(raw.get("salutation")),
            "raw_email": clean_text(raw.get("emailAddress")),
            "raw_alt_emails_json": _json(alt_emails),
            "raw_business_phone": clean_text(raw.get("businessPhone")),
            "raw_alt_phones_json": _json(alt_phones),
            "raw_company": clean_text(raw.get("company")),
            "raw_job_title": clean_text(raw.get("jobTitle")),
            "raw_city": clean_text(address.get("city")),
            "raw_state": clean_text(address.get("state")).upper(),
            "raw_postal": clean_text(address.get("postalCode")),
            "raw_street": clean_text(address.get("line1")),
            "raw_tier": clean_text(custom.get("tier__a_b_c")),
            "norm_full_name": canonical.full, "norm_first_name": canonical.first,
            "norm_last_name": canonical.last, "norm_email": email,
            "email_domain": email.rpartition("@")[2],
            "norm_phone": normalize_phone(raw.get("businessPhone")),
            "structured_name_malformed": bool(not structured.first or not structured.last),
            "generic_email": is_generic_email(email),
            "email_claim_count": emails[email] if email else 0,
            "act_id_claim_count": ids[act_id] if act_id else 0,
            "crd_claim_count": crds[crd] if crd else 0,
            "record_active": raw.get("isActive") is not False,
        })
    return out


def evaluate_assertion(record: Mapping[str, Any],
                       sec: Optional[Mapping[str, Any]],
                       context: Optional[Mapping[str, Any]] = None) -> tuple[dict, dict]:
    """Return immutable evidence and an automatic fail-closed link decision."""
    context = context or {}
    raw_claim = clean_text(record.get("raw_claimed_crd"))
    claimed = normalize_crd(raw_claim)
    conflicts, warnings, positive = [], [], []
    if raw_claim and not claimed:
        conflicts.append("invalid_crd_format")
    if not raw_claim:
        warnings.append("no_asserted_crd")
    if claimed and sec is None:
        conflicts.append("crd_not_found_in_sec")
    if int(record.get("act_id_claim_count") or 0) != 1:
        conflicts.append("missing_or_duplicate_act_guid")
    if claimed and int(record.get("crd_claim_count") or 0) > 1:
        conflicts.append("duplicate_crd_claim")
    if int(record.get("email_claim_count") or 0) > 1:
        conflicts.append("duplicate_email_claim")
    if record.get("generic_email"):
        conflicts.append("generic_email")
    if not bool(record.get("record_active", True)):
        conflicts.append("inactive_act_record")

    full_exact = structured_exact = given_exact = given_used = nickname = False
    suffix_ok = True
    if sec is not None:
        sec_last, act_last = name_token(sec.get("last_name")), name_token(record.get("norm_last_name"))
        full_exact = bool(act_last and sec_last and act_last == sec_last)
        structured_last = name_token(record.get("raw_last_name"))
        structured_exact = bool(structured_last and sec_last and structured_last == sec_last)
        if full_exact:
            positive.append("surname_exact")
        else:
            conflicts.append("surname_conflict_or_missing")
        sec_given = [sec.get("first_name"), sec.get("middle_name"), sec.get("used_first_name")]
        act_given = [record.get("norm_first_name"), record.get("raw_first_name")]
        agrees, given_reason = given_agreement(act_given, sec_given)
        given_exact = given_reason == "given_exact"
        given_used = bool(name_token(sec.get("used_first_name")) and
                          name_token(sec.get("used_first_name")) in
                          {name_token(x) for x in act_given})
        nickname = given_reason == "given_strict_nickname"
        if agrees:
            positive.append(given_reason)
        else:
            conflicts.append(given_reason)
        act_suffix = normalize_suffix(record.get("raw_suffix"))
        sec_suffix = normalize_suffix(sec.get("suffix"))
        suffix_ok = not act_suffix or not sec_suffix or act_suffix == sec_suffix
        if not suffix_ok:
            conflicts.append("suffix_conflict")
        if record.get("structured_name_malformed"):
            warnings.append("structured_act_name_malformed")

    email = normalize_email(record.get("norm_email"))
    if not email:
        warnings.append("missing_or_invalid_primary_email")
    current_firms = {clean_text(x) for x in context.get("current_firms", [])
                     if clean_text(x)}
    prior_firms = {clean_text(x) for x in context.get("prior_firms", [])
                   if clean_text(x)}
    current_firm_names = {name_token(x) for x in
                          context.get("current_firm_names", []) if name_token(x)}
    prior_firm_names = {name_token(x) for x in
                        context.get("prior_firm_names", []) if name_token(x)}
    branches = list(context.get("branches", []))
    act_street, act_city = (name_token(record.get("raw_street")),
                            name_token(record.get("raw_city")))
    act_state = clean_text(record.get("raw_state")).upper()
    act_postal = clean_text(record.get("raw_postal"))[:5]
    street_exact = bool(act_street and any(
        name_token(b.get("street")) == act_street for b in branches))
    city_exact = bool(act_city and any(
        name_token(b.get("city")) == act_city for b in branches))
    state_exact = bool(act_state and any(
        clean_text(b.get("state")).upper() == act_state for b in branches))
    postal_exact = bool(act_postal and any(
        clean_text(b.get("postal"))[:5] == act_postal for b in branches))
    for label, present in (("sec_branch_street_exact", street_exact),
                           ("sec_branch_city_exact", city_exact),
                           ("sec_branch_state_exact", state_exact),
                           ("sec_branch_postal_exact", postal_exact)):
        if present:
            positive.append(label)
    roster = list(context.get("roster", []))
    roster_phone_exact = bool(record.get("norm_phone") and any(
        normalize_phone(r.get("phone")) == record.get("norm_phone")
        for r in roster))
    sec_last = name_token((sec or {}).get("last_name"))
    roster_name_agrees = False
    for item in roster:
        parts = clean_text(item.get("name")).split()
        roster_last = name_token(item.get("name_last") or
                                 (parts[-1] if parts else ""))
        agrees = given_agreement(
            [item.get("name_first"), item.get("name")],
            [(sec or {}).get("first_name"), (sec or {}).get("middle_name"),
             (sec or {}).get("used_first_name")])[0]
        roster_name_agrees = roster_name_agrees or bool(
            agrees and roster_last and roster_last == sec_last)
    roster_firm_agrees = bool(current_firms and any(
        clean_text(r.get("firm_crd")) in current_firms for r in roster))
    act_company = name_token(record.get("raw_company"))
    act_firm_current = bool(act_company and act_company in current_firm_names)
    act_firm_prior = bool(act_company and act_company in prior_firm_names)
    if act_firm_current:
        positive.append("act_company_current_firm_exact")
    elif act_firm_prior:
        positive.append("act_company_prior_firm_exact")
    if roster:
        positive.append("independent_roster_email_exact")
    if roster_phone_exact:
        positive.append("independent_roster_phone_exact")
    if roster_name_agrees:
        positive.append("independent_roster_name_agrees")
    if roster_firm_agrees:
        positive.append("independent_roster_firm_current_agrees")
    # The CRD and name are both claims from ACT. Automatic approval needs a
    # second source connecting this ACT row to the SEC person.
    independently_corroborated = bool(
        act_firm_current or street_exact or postal_exact or
        (city_exact and state_exact) or roster_name_agrees)
    firm_comparable = bool(act_company and current_firm_names)
    firm_contradiction = bool(
        firm_comparable and not act_firm_current and not act_firm_prior)
    postal_comparable = bool(act_postal and any(
        clean_text(b.get("postal"))[:5] for b in branches))
    street_comparable = bool(act_street and any(
        name_token(b.get("street")) for b in branches))
    city_state_comparable = bool(act_city and act_state and any(
        name_token(b.get("city")) and clean_text(b.get("state"))
        for b in branches))
    address_contradiction = bool(
        (postal_comparable and not postal_exact) or
        (street_comparable and not street_exact) or
        (city_state_comparable and not (city_exact and state_exact)))
    if firm_contradiction:
        warnings.append("current_firm_differs")
    if address_contradiction:
        warnings.append("current_address_differs")
    conflicts, warnings, positive = map(lambda v: sorted(set(v)),
                                        (conflicts, warnings, positive))
    payload = {
        "recordKey": clean_text(record.get("record_key")),
        "actId": clean_text(record.get("source_record_id")),
        "claimedCrdRaw": raw_claim, "claimedCrd": claimed,
        "actName": clean_text(record.get("norm_full_name")),
        "actEmail": email, "actCompany": clean_text(record.get("raw_company")),
        "secName": sec_name(sec or {}),
        "crdClaimCount": int(record.get("crd_claim_count") or 0),
        "emailClaimCount": int(record.get("email_claim_count") or 0),
        "actIdClaimCount": int(record.get("act_id_claim_count") or 0),
        "recordActive": bool(record.get("record_active", True)),
        "conflicts": conflicts, "warnings": warnings,
        "candidate": context.get("candidate", {}),
        "currentFirms": sorted(current_firms),
        "currentFirmNames": sorted(current_firm_names),
        "priorFirms": sorted(prior_firms),
        "priorFirmNames": sorted(prior_firm_names),
        "branches": branches, "rosterEvidence": roster,
    }
    ev_hash = evidence_hash(payload)
    evidence = {
        "record_key": record.get("record_key", ""),
        "source_record_id": record.get("source_record_id", ""),
        "advisor_crd": claimed if sec is not None else "", "claimed_crd": claimed,
        "candidate_origin": "act_asserted_crd" if raw_claim else (
            "legacy_crosswalk_suggestion" if context.get("candidate") else "none"),
        "candidate_crd": normalize_crd(
            context.get("candidate", {}).get("advisor_crd")),
        "candidate_tier": clean_text(context.get("candidate", {}).get("tier")),
        "candidate_score": float(context.get("candidate", {}).get("match_score") or 0),
        "candidate_gap": float(context.get("candidate", {}).get("match_gap") or 0),
        "crd_exists": bool(sec), "full_surname_exact": full_exact,
        "structured_surname_exact": structured_exact, "given_exact": given_exact,
        "given_sec_used": given_used, "given_strict_nickname": nickname,
        "suffix_agrees": suffix_ok, "act_given_name": record.get("norm_first_name", ""),
        "sec_first_name": clean_text((sec or {}).get("first_name")),
        "sec_middle_name": clean_text((sec or {}).get("middle_name")),
        "sec_used_first_name": clean_text((sec or {}).get("used_first_name")),
        "sec_last_name": clean_text((sec or {}).get("last_name")),
        "sec_suffix": clean_text((sec or {}).get("suffix")),
        "email_valid": bool(email),
        "email_unique_in_act": int(record.get("email_claim_count") or 0) <= 1,
        "email_personal": bool(email and not record.get("generic_email")),
        "email_surname_exact": False, "email_given_exact": False,
        "current_firms_json": _json(current_firms),
        "prior_firms_json": _json(prior_firms),
        "firm_current_agrees": roster_firm_agrees or act_firm_current,
        "firm_prior_agrees": act_firm_prior,
        "sec_branches_json": json.dumps(branches, sort_keys=True,
                                         separators=(",", ":")),
        "street_exact": street_exact, "city_exact": city_exact,
        "state_exact": state_exact, "postal_exact": postal_exact,
        "roster_evidence_json": json.dumps(roster, sort_keys=True,
                                            separators=(",", ":")),
        "roster_email_exact": bool(roster),
        "roster_phone_exact": roster_phone_exact,
        "roster_name_agrees": roster_name_agrees,
        "roster_firm_current_agrees": roster_firm_agrees,
        "positive_evidence_json": _json(positive),
        "hard_conflicts_json": _json(conflicts), "warnings_json": _json(warnings),
        "evidence_hash": ev_hash, "ruleset_version": RULESET_VERSION,
    }
    if not raw_claim:
        status, reason = "unmatched", "no_asserted_crd"
    elif conflicts:
        status, reason = "quarantine", conflicts[0]
    elif not independently_corroborated:
        if firm_contradiction:
            status, reason = "review", "current_firm_conflict_review"
        elif address_contradiction:
            status, reason = "review", "current_address_conflict_review"
        else:
            status, reason = "review", "independent_corroboration_required"
    else:
        status, reason = "approved", "validated_act_asserted_crd"
    preferred_status, preferred_reason = preferred_name_status(
        record.get("raw_salutation"), (sec or {}).get("first_name"),
        (sec or {}).get("used_first_name"))
    greeting = clean_text(record.get("raw_salutation")) if preferred_status == "approved_auto" else (
        clean_text((sec or {}).get("used_first_name")) or clean_text((sec or {}).get("first_name")))
    legal = sec_name(sec or {})
    link = {
        "record_key": record.get("record_key", ""), "source_system": "act",
        "source_record_id": record.get("source_record_id", ""), "claimed_crd": claimed,
        "advisor_crd": claimed if sec is not None else "", "identity_status": status,
        "confidence_class": "source_assertion_validated" if status == "approved" else "none",
        "decision_reason": reason, "hard_conflicts_json": _json(conflicts),
        "warnings_json": _json(warnings), "can_display_contact": status == "approved",
        "can_call": status == "approved" and bool(record.get("norm_phone")),
        "can_email": status == "approved" and bool(email), "can_sync_act": status == "approved",
        "legal_name": legal, "sec_used_name": clean_text((sec or {}).get("used_first_name")),
        "act_name": record.get("norm_full_name", ""),
        "act_first_name": record.get("norm_first_name", ""),
        "act_last_name": record.get("norm_last_name", ""), "email": email,
        "firm": record.get("raw_company", ""), "tier": "confirmed" if status == "approved" else "",
        "preferred_first": clean_text(record.get("raw_salutation")),
        "preferred_status": preferred_status, "preferred_reason": preferred_reason,
        "display_name": legal or record.get("norm_full_name", ""), "email_greeting": greeting,
        "assertion_evidence_hash": ev_hash, "resolved_evidence_hash": ev_hash,
        "ruleset_version": RULESET_VERSION, "decision_source": "automatic_rules",
        "reviewed_by": "", "reviewed_utc": "",
    }
    return evidence, link


def apply_decision(link: dict, decision: Mapping[str, Any],
                   sec_by_crd: Mapping[str, Mapping[str, Any]]) -> dict:
    """Apply a hash-bound human decision without mutating its input."""
    out = dict(link)
    if clean_text(decision.get("expected_evidence_hash")) != link.get("assertion_evidence_hash"):
        return out
    choice = clean_text(decision.get("link_decision")).lower()
    if "inactive_act_record" in link.get("hard_conflicts_json", ""):
        return out
    resolved = normalize_crd(decision.get("resolved_crd")) or link.get("advisor_crd", "")
    if choice in {"approve", "replace"} and resolved in sec_by_crd:
        duplicate = "duplicate_crd_claim" in link.get("hard_conflicts_json", "")
        if duplicate and not bool(decision.get("canonical_for_crd")):
            return out
        sec = sec_by_crd[resolved]
        preferred_status, preferred_reason = preferred_name_status(
            link.get("preferred_first"), sec.get("first_name"),
            sec.get("used_first_name"))
        fallback_greeting = (
            clean_text(link.get("preferred_first"))
            if preferred_status == "approved_auto"
            else (clean_text(sec.get("used_first_name")) or
                  clean_text(sec.get("first_name")))
        )
        out.update({
            "advisor_crd": resolved, "identity_status": "approved",
            "confidence_class": "human_reviewed", "decision_reason": "human_review_approved",
            "can_display_contact": True, "can_call": True,
            "can_email": bool(link.get("email")), "can_sync_act": True,
            "legal_name": sec_name(sec),
            "sec_used_name": clean_text(sec.get("used_first_name")),
            "display_name": sec_name(sec), "tier": "confirmed",
            "preferred_status": preferred_status,
            "preferred_reason": preferred_reason,
            "email_greeting": fallback_greeting,
            "decision_source": "human_review",
            "reviewed_by": clean_text(decision.get("reviewer")),
            "reviewed_utc": clean_text(decision.get("reviewed_utc")),
        })
    elif choice == "reject":
        out.update({
            "identity_status": "rejected", "confidence_class": "human_reviewed",
            "decision_reason": "human_review_rejected", "can_display_contact": False,
            "can_call": False, "can_email": False, "can_sync_act": False,
            "decision_source": "human_review",
            "reviewed_by": clean_text(decision.get("reviewer")),
            "reviewed_utc": clean_text(decision.get("reviewed_utc")),
        })
    pref_choice = clean_text(decision.get("preferred_decision")).lower()
    if out.get("identity_status") == "approved" and pref_choice == "approve":
        preferred = clean_text(decision.get("preferred_first"))
        if preferred and name_token(preferred) not in {
                "advisor", "assistant", "contact", "office", "team"}:
            out.update({"preferred_first": preferred,
                        "preferred_status": "approved_reviewed",
                        "preferred_reason": "human_review_approved",
                        "email_greeting": preferred,
                        "decision_source": "human_review",
                        "reviewed_by": clean_text(decision.get("reviewer")),
                        "reviewed_utc": clean_text(decision.get("reviewed_utc"))})
    elif pref_choice == "reject":
        sec = sec_by_crd.get(out.get("advisor_crd"), {})
        out.update({"preferred_status": "rejected_reviewed",
                    "preferred_reason": "human_review_rejected",
                    "email_greeting": out.get("sec_used_name") or
                                      clean_text(sec.get("first_name")),
                    "decision_source": "human_review",
                    "reviewed_by": clean_text(decision.get("reviewer")),
                    "reviewed_utc": clean_text(decision.get("reviewed_utc"))})
    out["resolved_evidence_hash"] = evidence_hash({
        "assertionEvidenceHash": link.get("assertion_evidence_hash", ""),
        "decisionHash": clean_text(decision.get("decision_hash")),
        "resolvedCrd": out.get("advisor_crd", ""),
        "preferred": out.get("email_greeting", ""),
    })
    return out
