"""Bounded minor-issue policy for unattended data publication."""
from __future__ import annotations
import re
from refresh_core import RefreshError

MINOR_WARNINGS = (
    re.compile(r"^ACT account-value discrepancies: \d+ broker codes;"),
    re.compile(r"^Roster identity review: \d+ source rows held rather than reassigned"),
    re.compile(r"^Automatic last-good source reuse: (?:roster:|barrons|forbes)"),
    re.compile(r"^Explicitly reused last-good sources after failure: "),
    re.compile(r"^Minor current-SEC map gaps: \d+ of \d+"),
)
CRITICAL_SOURCES = {"act", "sec_individuals", "sec_firms", "sec_adv"}

def minor_warnings(warnings):
    unknown = [str(w) for w in warnings if not any(pattern.match(str(w)) for pattern in MINOR_WARNINGS)]
    if unknown:
        raise RefreshError("Unclassified warnings require review before automatic publication")
    return list(warnings)

def can_reuse_source(source_id, files):
    return bool(files) and (source_id.startswith("roster:") or source_id in {"barrons", "forbes"})

def check_source_results(sources):
    if any(row.get("status") not in {"refreshed","reused_last_good"} for row in sources.values()):
        raise RefreshError("An unresolved source refresh requires review")
    reused = [sid for sid,row in sources.items() if row.get("status")=="reused_last_good"]
    if any(sid in CRITICAL_SOURCES for sid in reused):
        raise RefreshError("A critical source did not refresh")
    optional = [sid for sid in sources if sid not in CRITICAL_SOURCES]
    if reused and len(reused) > max(1, len(optional) * 0.25):
        raise RefreshError("Too many source refreshes failed for unattended publication")

def minor_map_reason(comparison, drops, limit_percent=1):
    if drops != ["previously_mapped_current_SEC"]: return None
    row = comparison.get("mapContinuity", {})
    missing = row.get("missingFromCandidate", 0)
    total = row.get("previouslyMappedStillInSEC", 0)
    if not isinstance(total,(int,float)) or not total or not isinstance(missing,(int,float)):
        return None
    if row.get("missingWithUsablePlacement") != 0: return None
    if missing <= 0 or missing / total * 100 > limit_percent: return None
    return ("Minor current-SEC map gaps: %d of %d (%.3f%%); current-address cache coverage. "
            "Full work-location, data, asset and identity checks remain required.") % (missing,total,missing/total*100)
