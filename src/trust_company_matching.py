"""Fail-closed institution matching for trust-company research."""
from __future__ import annotations

import collections
import hashlib
import re

from trust_company_schema import (
    OFFICIAL_KINDS, SOURCE_PRIORITY, STRONG_IDS, Record,
)


def strong_ids(record: Record) -> dict[str, str]:
    return {field: getattr(record, field) for field in STRONG_IDS
            if getattr(record, field)}


def record_key(record: Record) -> str:
    return f"{record.source_kind}:{record.source_name}:{record.source_record_id}"


def pair_links(records: list[Record]) -> list[dict[str, str]]:
    """Suggest links; names and domains are never automatic identity evidence."""
    pairs: dict[tuple[int, int], set[str]] = collections.defaultdict(set)
    ids: dict[tuple[str, str], list[int]] = collections.defaultdict(list)
    names: dict[tuple[str, str], list[int]] = collections.defaultdict(list)
    domains: dict[str, list[int]] = collections.defaultdict(list)
    for index, record in enumerate(records):
        for kind, value in strong_ids(record).items():
            ids[(kind, value)].append(index)
        if record.normalized_name:
            names[(record.normalized_name, record.state)].append(index)
        if record.domain:
            domains[record.domain].append(index)

    def add(indices: list[int], evidence: str) -> None:
        if len(indices) > 20:
            return
        for position, left in enumerate(indices):
            for right in indices[position + 1:]:
                if ((records[left].source_kind, records[left].source_name) !=
                        (records[right].source_kind, records[right].source_name)):
                    pairs[tuple(sorted((left, right)))].add(evidence)

    for (kind, value), indices in ids.items():
        add(indices, f"shared_{kind}:{value}")
    for (name, state), indices in names.items():
        add(indices, f"exact_normalized_name_state:{name}|{state}")
    for domain, indices in domains.items():
        add(indices, f"shared_domain:{domain}")

    output = []
    for (left, right), evidence in sorted(pairs.items()):
        a, b = records[left], records[right]
        shared = set(strong_ids(a).items()) & set(strong_ids(b).items())
        conflicts = sorted(field for field in STRONG_IDS
                           if getattr(a, field) and getattr(b, field)
                           and getattr(a, field) != getattr(b, field))
        if shared and not conflicts:
            action, confidence = "auto_link", "strong_identifier"
        elif shared:
            action, confidence = "block_conflict", "conflicting_identifiers"
        elif (a.normalized_name == b.normalized_name and a.state == b.state
              and a.domain and a.domain == b.domain):
            action, confidence = "review", "name_state_domain"
        elif a.normalized_name == b.normalized_name and a.state == b.state:
            action, confidence = "review", "name_and_state"
        else:
            action, confidence = "review", "domain_only"
        output.append({
            "left_record": record_key(a), "right_record": record_key(b),
            "left_name": a.legal_name, "right_name": b.legal_name,
            "action": action, "confidence": confidence,
            "evidence": "|".join(sorted(evidence)), "conflicts": "|".join(conflicts),
        })
    return output


class UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def cluster_records(records: list[Record], links: list[dict[str, str]]) -> list[list[Record]]:
    positions = {record_key(record): index for index, record in enumerate(records)}
    graph = UnionFind(len(records))
    for link in links:
        if link["action"] == "auto_link":
            graph.union(positions[link["left_record"]], positions[link["right_record"]])
    groups: dict[int, list[Record]] = collections.defaultdict(list)
    for index, record in enumerate(records):
        groups[graph.find(index)].append(record)
    clusters = []
    for group in groups.values():
        conflict = any(len({getattr(record, field) for record in group
                            if getattr(record, field)}) > 1 for field in STRONG_IDS)
        clusters.extend([[record] for record in group] if conflict else [group])
    return sorted(clusters, key=lambda group: min(record_key(record) for record in group))


def joined(group: list[Record], field: str) -> str:
    return "|".join(sorted({getattr(record, field) for record in group
                            if getattr(record, field)}))


SPECIALIZED_NON_ADVISOR = re.compile(
    r"\b(?:digital|crypto|paxos|bitgo|corporate trust|college trust|"
    r"employee benefit|retirement|paycom|adp trust)\b", re.I)
CLIENT_FACING_SIGNAL = re.compile(
    r"\b(?:wealth|advisor|advisory|private client|private trust|"
    r"investment|bessemer|glenmede|rockefeller|hightower|stifel|"
    r"neuberger|brown brothers)\b", re.I)


def is_current_official(record: Record) -> bool:
    return record.source_kind in OFFICIAL_KINDS and record.status == "active"


def prospect_classification(group: list[Record]) -> tuple[str, str, str]:
    """Classify for research triage, never production eligibility."""
    name = " ".join(record.legal_name for record in group)
    public_private = joined(group, "public_private").lower()
    official = any(is_current_official(record) for record in group)
    has_13f = any(record.source_kind == "sec_13f" for record in group)
    if "private" in public_private and "public" not in public_private:
        return ("excluded", "private_family_trust",
                "regulator classifies the institution as private; not a public sales prospect")
    if SPECIALIZED_NON_ADVISOR.search(name):
        return ("excluded", "specialized_non_advisor",
                "name indicates digital asset, corporate, retirement, employee, or college trust work")
    if official and has_13f:
        return ("priority_candidate", "regulated_investment_manager",
                "regulator evidence and a current Form 13F investment-management signal")
    if official and CLIENT_FACING_SIGNAL.search(name):
        return ("candidate", "likely_client_facing_wealth",
                "regulated institution with a name suggesting wealth or investment services")
    if official:
        return ("candidate", "regulated_trust_needs_business_review",
                "regulated trust institution; client-facing investment relevance needs review")
    if has_13f:
        return ("candidate", "13f_trust_named_manager",
                "Trust-named Form 13F manager; regulator confirmation is still required")
    return ("excluded", "insufficient_evidence", "no regulator or investment-management signal")


def institution_rows(clusters: list[list[Record]]) -> list[dict[str, object]]:
    output = []
    for group in clusters:
        ordered = sorted(group, key=lambda record: (
            SOURCE_PRIORITY.get(record.source_kind, 9), record.source_name,
            record.source_record_id))
        canonical = ordered[0]
        fingerprint = "|".join(sorted(record_key(record) for record in group))
        official = [record for record in group if is_current_official(record)]
        historical = [record for record in group
                      if record.source_kind in OFFICIAL_KINDS and record.status != "active"]
        prospect_status, prospect_bucket, prospect_reason = prospect_classification(group)
        output.append({
            "research_id": "trust-" + hashlib.sha256(fingerprint.encode()).hexdigest()[:16],
            "legal_name": canonical.legal_name,
            "alternate_names": "|".join(sorted({record.legal_name for record in group
                                                  if record.legal_name != canonical.legal_name})),
            "institution_type": official[0].institution_type if official else canonical.institution_type,
            "regulatory_status": ("verified_trust_company" if official else
                                  "historical_or_inactive" if historical else "candidate_only"),
            "identity_status": ("regulator_verified" if official else
                                "historical_or_inactive" if historical else
                                "unverified_candidate"),
            "needs_identity_review": not bool(official),
            "needs_business_review": prospect_bucket.endswith("review") or not bool(official),
            "prospect_status": prospect_status,
            "prospect_bucket": prospect_bucket,
            "prospect_reason": prospect_reason,
            "state": canonical.state, "city": canonical.city,
            "website": joined(group, "website"), "domain": joined(group, "domain"),
            "license_scope": joined(group, "license_scope"),
            "public_private": joined(group, "public_private"),
            "source_snapshot_dates": joined(group, "source_snapshot_date"),
            "rssd": joined(group, "rssd"), "cik": joined(group, "cik"),
            "lei": joined(group, "lei"), "ein": joined(group, "ein"),
            "fdic_cert": joined(group, "fdic_cert"),
            "occ_charter": joined(group, "occ_charter"),
            "state_charter": joined(group, "state_charter"),
            "form13f_file": joined(group, "form13f_file"),
            "reportable_13f_value_dollars": joined(group, "reportable_13f_value"),
            "has_13f_signal": any(record.source_kind == "sec_13f" for record in group),
            "crd_observed_not_identity_key": joined(group, "crd"),
            "source_count": len({record.source_name for record in group}),
            "record_count": len(group),
            "sources": "|".join(sorted({record.source_name for record in group})),
            "source_records": "|".join(sorted(record_key(record) for record in group)),
        })
    return output
