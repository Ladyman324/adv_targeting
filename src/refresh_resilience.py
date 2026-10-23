"""Source-specific historical gates. Never publish or repair contact data here."""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from pathlib import Path
import re
import shutil
import unicodedata

import refresh_core as core

POLICY_VERSION = "edward-jones-history-v1"
REQUIRED = {"fid", "faName", "faUrl", "phone", "faCity", "faState", "email"}
TRACKED = {"name": "faName", "email": "email", "phone": "phone",
           "city": "faCity", "state": "faState", "address": "address", "profile": "faUrl"}


class HistoricalHold(core.RefreshError):
    """Safe reference to a held snapshot, without source content or paths."""
    def __init__(self, label, source_id, comparison, codes):
        self.source_id, self.comparison_id, self.issue_codes = source_id, comparison, sorted(set(codes))
        super().__init__(label + " historical gate held candidate: " + ", ".join(self.issue_codes)
                         + ". Review reference: " + comparison)


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def _load(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)) or not REQUIRED.issubset(fields):
            raise core.RefreshError("Historical gate: missing or duplicate Edward Jones columns")
        rows = list(reader)
    if not rows or any(None in row or any(v is None for v in row.values()) for row in rows):
        raise core.RefreshError("Historical gate: empty or truncated Edward Jones CSV")
    return rows


def _id(row):
    return normalized(row.get("fid"))


def _email(row):
    # The scraper's own-person email, not all addresses found on a team page.
    value = normalized(row.get("email"))
    return value if re.fullmatch(r"[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+", value) else ""


def _values(row):
    values = {label: normalized(row.get(field)) for label, field in TRACKED.items()}
    values["phone"] = re.sub(r"\D", "", values["phone"])
    values["email"] = _email(row)
    return values


def _summary(rows):
    counts = Counter(_id(row) for row in rows)
    valid = {key: _values(row) for row in rows if (key := _id(row)) and counts[key] == 1}
    presence = Counter()
    for row in rows:
        presence.update({key: bool(value) for key, value in _values(row).items()})
    return valid, {"records": len(rows), "uniquePeople": len(valid), "missingIds": counts.get("", 0),
                   "duplicateIdRows": sum(n for key, n in counts.items() if key and n > 1),
                   "fieldsPresent": dict(presence)}


def _compare_edward_jones(candidate, baseline=None):
    """Pure, checksum-bound assessment. Ambiguous changes are held for review.

    No coverage override bypasses this gate; missing people are observations,
    not confirmed departures. Reports omit names, email addresses and raw IDs.
    """
    candidate = Path(candidate)
    candidate_hash = core.file_hash(candidate)
    baseline_hash = core.file_hash(baseline) if baseline else None
    result = {"schemaVersion": 1, "policyVersion": POLICY_VERSION, "sourceId": "roster:edward_jones",
              "candidateSha256": candidate_hash, "baselineSha256": baseline_hash,
              "status": "held", "issues": [], "thresholds": {
                  "maxMissingPeopleFraction": .05, "maxAddedPeopleFraction": .20,
                  "maxChangedFieldFraction": .10, "maxFieldLossFraction": .05,
                  "maxMissingStateFraction": .20}}
    def issue(code, **facts):
        result["issues"].append({"code": code, **facts})
    try:
        rows = _load(candidate)
        current, summary = _summary(rows)
    except (core.RefreshError, csv.Error, UnicodeError) as exc:
        issue("CSV_FORMAT_INVALID", errorType=type(exc).__name__)
        return result
    result["candidate"] = summary
    if summary["missingIds"] or summary["duplicateIdRows"]:
        issue("PERSON_IDS_INVALID", missing=summary["missingIds"], duplicates=summary["duplicateIdRows"])
    for field in ("name", "profile"):
        if summary["fieldsPresent"][field] != summary["records"]:
            issue("REQUIRED_PERSON_FIELD_MISSING", field=field)
    invalid = sum(bool(normalized(row.get("email"))) and not _email(row) for row in rows)
    if invalid:
        issue("INVALID_EMAIL_VALUES", count=invalid)
    pending = sum(normalized(row.get("email_status")) not in {
        "", "ok", "field_missing", "field_empty", "not_found"} for row in rows)
    if pending:
        issue("PROFILE_ACQUISITION_INCOMPLETE", count=pending)
    if not baseline:
        issue("BASELINE_REVIEW_REQUIRED")
        return result
    try:
        old, old_summary = _summary(_load(baseline))
    except (core.RefreshError, csv.Error, UnicodeError) as exc:
        issue("BASELINE_INVALID", errorType=type(exc).__name__)
        return result
    result["baseline"] = old_summary
    if old_summary["missingIds"] or old_summary["duplicateIdRows"]:
        issue("BASELINE_PERSON_IDS_INVALID")
    common = set(old) & set(current)
    removed, added = set(old) - set(current), set(current) - set(old)
    result["continuity"] = {"retained": len(common), "removed": len(removed), "added": len(added)}
    if len(removed) > len(old) * .05:
        issue("PEOPLE_DISAPPEARED", count=len(removed))
    if len(added) > max(1, len(old) * .20):
        issue("UNEXPECTED_PEOPLE_GROWTH", count=len(added))
    changes, losses, unchanged = Counter(), Counter(), 0
    for key in sorted(common):
        previous, following = old[key], current[key]
        unchanged += previous == following
        for field in TRACKED:
            changes[field] += previous[field] != following[field]
            losses[field] += bool(previous[field]) and not following[field]
    result.update(unchangedPeople=unchanged, changedFields=dict(changes), lostFields=dict(losses))
    for field in TRACKED:
        populated = sum(bool(old[key][field]) for key in common)
        if losses[field] > populated * .05:
            issue("FIELD_VALUES_LOST", field=field, count=losses[field])
        if changes[field] > len(common) * .10:
            issue("FIELD_VALUES_CHANGED", field=field, count=changes[field])
    owners, new_owners = defaultdict(set), defaultdict(set)
    for key, row in old.items():
        if row["email"]:
            owners[row["email"]].add(key)
    moved = 0
    for key, row in current.items():
        if row["email"]:
            email = row["email"]
            new_owners[email].add(key)
            moved += bool(owners[email]) and key not in owners[email]
    if moved:
        issue("EMAIL_PERSON_ASSOCIATION_CHANGED", count=moved)
    expanded = sum(len(keys) > 1 and keys != owners[email] for email, keys in new_owners.items())
    if expanded:
        issue("NEW_SHARED_EMAIL_ASSOCIATIONS", count=expanded)
    states_before = Counter(row["state"] for row in old.values())
    states_after = Counter(row["state"] for row in current.values())
    for state, count in states_before.items():
        if state and count >= 5 and states_after[state] < count * .8:
            issue("STATE_COVERAGE_LOST", state=state, before=count, after=states_after[state])
    if core.file_hash(candidate) != candidate_hash or core.file_hash(baseline) != baseline_hash:
        raise core.RefreshError("Historical gate inputs changed during comparison")
    result["status"] = "held" if result["issues"] else "accepted"
    return result


def compare_edward_jones(candidate, baseline=None):
    # Check even the early format/error paths: evidence must bind the bytes
    # assessed, including a baseline that proved invalid or absent.
    candidate_hash = core.file_hash(candidate)
    baseline_hash = core.file_hash(baseline) if baseline else None
    result = _compare_edward_jones(candidate, baseline)
    if (result["candidateSha256"] != candidate_hash or result["baselineSha256"] != baseline_hash
            or core.file_hash(candidate) != candidate_hash
            or (core.file_hash(baseline) if baseline else None) != baseline_hash):
        raise core.RefreshError("Historical gate inputs changed during comparison")
    return result


def comparison_id(report):
    payload = {"policy": report["policyVersion"], "candidate": report["candidateSha256"],
               "baseline": report["baselineSha256"]}
    for key in ("comparisonOptions", "baselineFacts"):
        if key in report:
            payload[key] = report[key]
    return core.digest(payload)


def gate_roster(stage, source, slug, baseline):
    """Held bytes/evidence never enter the selected raw roster folder."""
    if slug == "edward_jones":
        report = compare_edward_jones(source, baseline)
    else:
        from refresh_history import compare_source
        report = compare_source(stage, "roster:" + slug, source)
    return enforce_history(stage, source, "roster:" + slug, report)


def gate_source(stage, source, source_id, options=None):
    from refresh_history import compare_source
    return enforce_history(stage, source, source_id, compare_source(stage, source_id, source, options))


def enforce_history(stage, source, source_id, report):
    from refresh_history import source_key, held_name, baseline_paths
    slug = source_key(source_id)
    identity = comparison_id(report)
    folder = Path(stage) / "data/refresh/acquisition_reviews" / slug / identity
    folder.mkdir(parents=True, exist_ok=True)
    evidence = folder / "comparison.json"
    if evidence.exists() and core.read_json(evidence) != report:
        raise core.RefreshError("Immutable historical comparison evidence changed")
    if not evidence.exists():
        core.atomic_json(evidence, report)
    decision_hash = None
    if report["status"] != "accepted":
        target = folder / held_name(source_id, report.get("comparisonOptions"))
        if not target.exists():
            shutil.copyfile(source, target)
        if core.file_hash(target) != report["candidateSha256"]:
            raise core.RefreshError("Historical gate held candidate hash mismatch")
        if source_id == "roster:edward_jones":
            from refresh_repair import queue_request
            queue_request(stage, report)
        from refresh_review import accepted_decision
        decision_hash = accepted_decision(folder, report)
        if not decision_hash:
            codes = sorted({item["code"] for item in report["issues"]})
            label = {"edward_jones": "Edward Jones", "sec_adv": "SEC ADV", "sec_individuals": "SEC individuals", "sec_firms": "SEC firms"}.get(slug, slug)
            raise HistoricalHold(label, source_id, identity, codes)
    baseline_facts = report.get("baselineFacts")
    if baseline_facts is None:
        baseline_facts = core.facts(baseline_paths(stage, source_id), stage)
    return {"policyVersion": report["policyVersion"], "comparisonId": identity,
            "baselineSha256": report["baselineSha256"], "candidateSha256": report["candidateSha256"],
            "warnings": report.get("warnings", []),
            "candidateRecords": report.get("candidate", {}).get("records"),
            "baselineFacts": baseline_facts,
            "reviewDecisionSha256": decision_hash,
            "status": "accepted"}
