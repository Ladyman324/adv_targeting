"""Shared firm-family and email-domain policy learned from normalized rosters."""
from __future__ import annotations

import collections
from typing import Iterable, Mapping

from firm_rosters import FIRMS, allowed_crds
from identity_normalize import is_generic_email, normalize_crd, normalize_email

FREE_DOMAINS = frozenset({
    "aol.com", "gmail.com", "hotmail.com", "icloud.com", "live.com",
    "outlook.com", "proton.me", "protonmail.com", "yahoo.com",
})


def crd_tuple(value) -> tuple[str, ...]:
    """Canonical tuple from a sequence or a pipe/comma-delimited string."""
    if isinstance(value, (set, frozenset, list, tuple)):
        values = value
    else:
        values = str(value or "").replace(",", "|").split("|")
    return tuple(sorted({normalize_crd(item) for item in values
                         if normalize_crd(item)}))


def build_domain_policy(rows: Iterable[Mapping], minimum_witnesses: int = 2
                        ) -> dict[str, dict]:
    """Return domain evidence; only compatible multi-witness rows are trusted."""
    seen = collections.defaultdict(lambda: {
        "emails": set(), "families": [], "sources": set(), "files": set()})
    for row in rows:
        email = normalize_email(row.get("email"))
        if not email or is_generic_email(email):
            continue
        domain = email.rsplit("@", 1)[1]
        if domain in FREE_DOMAINS:
            continue
        source_slug = str(row.get("source_slug") or "").strip()
        # A channel can narrow one roster ROW to one legal entity, but the
        # email DOMAIN still represents the configured firm family. Raymond
        # James is the production example: RJA rows carry 705 and IMD/FID rows
        # carry 149018. Treating those singleton rows as unrelated made
        # raymondjames.com ambiguous and disabled the ACT stale-CRD guard.
        configured = (set(crd_tuple(allowed_crds(source_slug)))
                      if source_slug in FIRMS else set())
        family = configured or set(crd_tuple(row.get("allowed_firm_crds")))
        if not family:
            continue
        item = seen[domain]
        item["emails"].add(email)
        if family not in item["families"]:
            item["families"].append(family)
        source = source_slug or str(row.get("source") or "").strip()
        source_file = str(row.get("source_file") or "").strip()
        if source:
            item["sources"].add(source)
        if source_file:
            item["files"].add(source_file)

    out = {}
    for domain, evidence in sorted(seen.items()):
        families = evidence["families"]
        # One connected component is required. Merely checking that every
        # family has some neighbour lets two unrelated overlapping clusters
        # pass as one domain (for example {1},{1,2} and {8},{8,9}).
        reached = {0} if families else set()
        while reached:
            expanded = reached | {
                index for index, family in enumerate(families)
                if any(family & families[known] for known in reached)
            }
            if expanded == reached:
                break
            reached = expanded
        connected = not families or len(reached) == len(families)
        family = set().union(*families) if families and connected else set()
        witnesses = len(evidence["emails"])
        status = ("authoritative" if connected and witnesses >= minimum_witnesses
                  else "ambiguous" if not connected else "insufficient")
        out[domain] = {
            "status": status,
            "allowedFirmCrds": sorted(family),
            "witnessCount": witnesses,
            "sources": sorted(evidence["sources"]),
            "sourceFiles": sorted(evidence["files"]),
        }
    return out


def authoritative_families(policy: Mapping[str, Mapping]) -> dict[str, tuple[str, ...]]:
    return {domain: crd_tuple(item.get("allowedFirmCrds"))
            for domain, item in policy.items()
            if item.get("status") == "authoritative"}
