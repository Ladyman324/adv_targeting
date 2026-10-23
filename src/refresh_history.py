"""Historical acquisition checks for source-native records, not CRM matching.

Stable provider identifiers are preferred. Name/location fallback is explicitly
reported as weaker continuity evidence and never creates a person/CRD link.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import re

import refresh_core as core
from refresh_resilience import normalized

FIELDS = {
    "name": ("name", "Name", "MarketingName", "Marketing Name", "Display Name"),
    "email": ("email", "Email", "emails", "Emails"),
    "phone": ("phone", "Phone", "Main Phone", "Formatted Phone", "LocalNumber", "phone_numbers", "direct_phone", "office_phone"),
    "city": ("city", "City", "office_city"), "state": ("state", "State", "office_state", "Region"),
    "address": ("address", "Address", "address1", "Address1", "Street Line 1", "office_address", "office_street", "address_line1", "branch_street"),
    "title": ("title", "Primary Title", "Job Title", "RankTitle", "JobTitle", "role"),
    "team": ("team_name", "Team Name", "TeamSiteNames", "group_team", "firm"),
    "profile": ("profile_url", "Profile URL", "advisor_profile_url", "Url", "url", "bio_url", "website_url", "Website URL", "website", "source_page"),
}
IDENTIFIERS = {
    "ubs": ("UniqueId", "EntityId"), "morgan_stanley": ("Yext ID",),
    "merrill": ("Advisor ID",), "raymond_james": ("advisor_id",),
    "ameriprise": ("advisor_id",), "baird": ("advisor_id", "external_id"),
    "cetera": ("advisor_crd", "advisor_id"), "northwestern_mutual": ("agent_number", "leid", "profile_slug"),
    "mercer": ("member_id",), "captrust": ("slug",), "ep_wealth": ("slug",),
    "focus_partners": ("slug",), "janney": ("slug",), "mariner": ("slug",), "stifel": ("slug",),
    "wealthspire": ("slug",), "chevy_chase_trust": ("slug",), "waverly": ("slug",), "red_door": ("slug",),
}
EMPTY = {"", "[]", "{}", "null", "none", "nan", "n/a"}
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def source_key(sid):
    from refresh_acquire import _source
    _source(sid)
    if sid == "act":
        raise core.RefreshError("ACT matching is not part of scraped-source historical review")
    return sid[7:] if sid.startswith("roster:") else sid


def baseline_paths(stage, sid):
    stage = Path(stage)
    if sid.startswith("roster:"):
        return sorted((stage / "data/raw/firm_rosters").glob(sid[7:] + "_*.csv"))[-1:]
    if sid in {"barrons", "forbes"}:
        canonical = stage / "data/raw" / (sid + "_rankings.json")
        history = sorted((stage / "data/refresh/ranking_editions" / sid).glob("*.json"))
        return ([canonical] if canonical.is_file() else []) + history
    if sid in {"sec_individuals", "sec_firms", "sec_adv"}:
        from refresh_inventory import _source_date
        raw = stage / "data/raw"
        if sid == "sec_adv":
            explicit = sorted(raw.glob("refresh_adv_*"))
            if len(explicit) > 1:
                raise core.RefreshError("Multiple explicit ADV baselines are ambiguous; select a baseline before refreshing")
            paths = explicit or list(raw.glob("ia??????.zip"))
        else:
            paths = list(raw.glob("IA_INDVL_Feed_*.xml.zip" if sid == "sec_individuals" else "IA_FIRM_SEC_Feed_*.xml.gz"))
        return sorted(paths, key=lambda path: (_source_date(path.name) or "", path.name))[-1:]
    raise core.RefreshError("Historical policy is not implemented for this source")


def held_name(sid, options=None):
    if sid.startswith("roster:"):
        return "held.csv"
    if sid in {"barrons", "forbes"}:
        return "held.json"
    if sid == "sec_individuals": return "held.xml.zip"
    if sid == "sec_firms": return "held.xml.gz"
    if sid == "sec_adv": return "held.zip" if (options or {}).get("format") == "zip" else "held.csv"
    raise core.RefreshError("Unsupported held source format")


def _read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        fields = reader.fieldnames or []
        if not fields or len(set(fields)) != len(fields):
            raise core.RefreshError("Missing or duplicate roster columns")
        rows = list(reader)
    if not rows or any(None in row or any(value is None for value in row.values()) for row in rows):
        raise core.RefreshError("Empty or truncated roster CSV")
    return fields, rows


def _value(row, aliases):
    for field in aliases:
        text = normalized(row.get(field))
        if text not in EMPTY:
            return text
    return ""


def _project(row):
    values = {field: _value(row, aliases) for field, aliases in FIELDS.items()}
    # Email collections vary by provider. These are same-row values only;
    # recognizing a string does not assert that an address is a person's own.
    values["email"] = tuple(sorted(set(EMAIL.findall(values["email"]))))
    values["phone"] = re.sub(r"\D", "", values["phone"])
    return values


def _identity(row, values, slug):
    for field in IDENTIFIERS.get(slug, ()):
        ident = _value(row, (field,))
        if ident:
            return "provider:" + field + ":" + ident, "provider"
    if slug == "sanctuary" and not values["name"]:
        return "practice:" + _value(row, ("domain", "source_page", "firm")), "practice"
    if values["profile"]:
        # Some sites publish team URLs. Include the name to avoid treating all
        # people sharing a team page as one provider person ID.
        return "profile:" + values["profile"] + ":" + values["name"], "profile_and_name"
    if values["name"]:
        return "name_location:" + ":".join(values[key] for key in ("name", "city", "state")), "name_location"
    return "", "missing"


def _roster_summary(rows, slug):
    people, kinds, presence, segments, duplicates, states = defaultdict(lambda: defaultdict(set)), Counter(), Counter(), Counter(), Counter(), Counter()
    email_owners = defaultdict(set)
    for row in rows:
        values = _project(row)
        ident, kind = _identity(row, values, slug)
        kinds[kind] += 1
        presence.update({field: bool(value) for field, value in values.items()})
        segment = _value(row, ("Entity Type", "record_type", "role"))
        if segment:
            segments[segment] += 1
        duplicates[core.digest({"identity": ident, "values": values})] += 1
        if re.fullmatch(r"[a-z]{2}", values["state"]):
            states[values["state"]] += 1
        if not ident:
            continue
        for field, value in values.items():
            if value:
                people[ident][field].add(value)
        if kind in {"provider", "profile_and_name"}:
            for email in values["email"]:
                email_owners[email].add(ident)
    summary = {"records": len(rows), "sourceEntities": len(people), "identityKinds": dict(kinds),
               "fieldsPresent": dict(presence), "segmentCounts": dict(segments),
               "stateCounts": dict(states),
               "duplicateRows": sum(n - 1 for n in duplicates.values() if n > 1)}
    return people, summary, email_owners


def compare_roster(candidate, baseline, slug):
    """Existing-baseline continuity gates; first imports receive schema checks.

    Exact duplicates already in the baseline are reported, not newly invented
    errors. New duplicates and provider-ID ambiguity are held for review.
    """
    sid = "roster:" + slug
    result = {"schemaVersion": 1, "policyVersion": "roster-history-v1:" + slug, "sourceId": sid,
              "candidateSha256": core.file_hash(candidate),
              "baselineSha256": core.file_hash(baseline) if baseline else None,
              "comparisonOptions": {}, "status": "held", "issues": [], "warnings": [],
              "thresholds": {"maxMissingEntityFraction": .20, "maxGrowthFraction": .25,
                             "maxFieldLossFraction": .05, "maxFieldChangeFraction": .10}}
    def issue(code, **facts): result["issues"].append({"code": code, **facts})
    try:
        fields, rows = _read_csv(candidate)
        from refresh_acquire import ROSTER_COLUMNS
        required = ROSTER_COLUMNS.get(slug, ({"name"}, (), ()))[0]
        if not set(required).issubset(fields):
            raise core.RefreshError("Required provider columns missing")
        current, summary, owners = _roster_summary(rows, slug)
        result["candidate"] = summary
    except (core.RefreshError, csv.Error, UnicodeError) as exc:
        issue("CSV_FORMAT_INVALID", errorType=type(exc).__name__)
        return result
    if summary["identityKinds"].get("missing"):
        issue("SOURCE_ENTITY_IDENTITY_MISSING", count=summary["identityKinds"]["missing"])
    if not baseline:
        result["warnings"].append("Initial snapshot: no historical continuity comparison is possible")
        result["status"] = "held" if result["issues"] else "accepted"
        return result
    try:
        old_fields, old_rows = _read_csv(baseline)
        old, old_summary, old_owners = _roster_summary(old_rows, slug)
    except (core.RefreshError, csv.Error, UnicodeError) as exc:
        issue("BASELINE_INVALID", errorType=type(exc).__name__)
        return result
    result["baseline"] = old_summary
    common = set(current) & set(old)
    result["continuity"] = {"retained": len(common), "removed": len(set(old) - set(current)), "added": len(set(current) - set(old))}
    if len(set(old) - set(current)) > len(old) * .20:
        issue("SOURCE_ENTITIES_DISAPPEARED", count=len(set(old) - set(current)))
    if len(set(current) - set(old)) > max(1, len(old) * .25):
        issue("UNEXPECTED_ENTITY_GROWTH", count=len(set(current) - set(old)))
    if summary["duplicateRows"] > old_summary["duplicateRows"]:
        issue("NEW_DUPLICATE_SOURCE_ROWS", count=summary["duplicateRows"] - old_summary["duplicateRows"])
    ambiguous = [key for key, values in current.items() if key.startswith("provider:")
                 and len(values.get("name", set())) > 1 and values.get("name") != old.get(key, {}).get("name")]
    if ambiguous:
        issue("SOURCE_ID_AMBIGUITY", count=len(ambiguous))
    changed = {}
    for field in FIELDS:
        before = old_summary["fieldsPresent"].get(field, 0)
        after = summary["fieldsPresent"].get(field, 0)
        if before and after / len(rows) < (before / len(old_rows)) * .95:
            issue("FIELD_COVERAGE_LOST", field=field, before=before, after=after)
        count = sum(old[key].get(field, set()) != current[key].get(field, set()) for key in common)
        changed[field] = count
        if count > len(common) * .10:
            issue("FIELD_VALUES_CHANGED", field=field, count=count)
    result["changedFields"] = changed
    reassigned = sum(bool(old_owners[email]) and not keys.issubset(old_owners[email]) for email, keys in owners.items())
    if reassigned:
        issue("EMAIL_PERSON_ASSOCIATION_CHANGED", count=reassigned)
    for segment, count in old_summary["segmentCounts"].items():
        if count >= 5 and summary["segmentCounts"].get(segment, 0) < count * .8:
            issue("SOURCE_SEGMENT_COVERAGE_LOST", before=count, after=summary["segmentCounts"].get(segment, 0))
    for state, count in old_summary["stateCounts"].items():
        if count >= 5 and summary["stateCounts"].get(state, 0) < count * .8:
            issue("STATE_COVERAGE_LOST", state=state, before=count, after=summary["stateCounts"].get(state, 0))
    if summary["identityKinds"].get("name_location"):
        result["warnings"].append("Name/location continuity is not a unique-person or CRD match")
    result["status"] = "held" if result["issues"] else "accepted"
    return result


def _ranking_groups(records, provider, categories=(), duplicate_metrics=None):
    groups = defaultdict(dict)
    for row in records:
        if not isinstance(row, dict):
            raise core.RefreshError("Ranking record must be an object")
        category = row.get("ranking" if provider == "barrons" else "list")
        person = row.get("advisor" if provider == "barrons" else "name")
        if not isinstance(category, str) or not category.strip() or not isinstance(person, str) or not person.strip():
            raise core.RefreshError("Ranking category/person missing")
        if categories and category not in categories:
            continue
        year = row.get("year")
        if year is not None and (isinstance(year, bool) or not str(year).isdigit() or not 1900 <= int(year) <= 2200):
            raise core.RefreshError("Ranking edition invalid")
        edition = int(year) if year is not None else None
        scope = normalized(row.get("state") if provider == "barrons" else row.get("category"))
        ident = (row.get("crd") or row.get("barrons_url") or person) if provider == "barrons" else (row.get("uri") or person)
        if not isinstance(ident, (str, int)):
            raise core.RefreshError("Ranking source identifier invalid")
        key, identity = (category, scope, edition), normalized(ident)
        if not identity:
            identity = normalized(person)
        if identity in groups[key] and duplicate_metrics is not None:
            duplicate_metrics["duplicates"] += 1
            duplicate_metrics["conflicts"] += core.digest(groups[key][identity]) != core.digest(row)
        groups[key][identity] = row
    return groups


def compare_rankings(candidate, baselines, provider, categories=()):
    """Edition transitions compare scope/coverage, never require same winners."""
    categories = tuple(sorted(set(categories)))
    result = {"schemaVersion": 1, "policyVersion": "rankings-history-v1:" + provider, "sourceId": provider,
              "candidateSha256": core.file_hash(candidate), "baselineSha256": None,
              "comparisonOptions": {"categories": list(categories)}, "status": "held", "issues": [], "warnings": []}
    def issue(code, **facts): result["issues"].append({"code": code, **facts})
    duplicates = {"duplicates": 0, "conflicts": 0}
    try:
        records = core.read_json(candidate)
        if not isinstance(records, list) or not records:
            raise core.RefreshError("Ranking snapshot must be a nonempty array")
        current = _ranking_groups(records, provider, categories, duplicates)
        if not current:
            raise core.RefreshError("No selected ranking categories")
    except (ValueError, TypeError, core.RefreshError) as exc:
        issue("RANKING_FORMAT_INVALID", errorType=type(exc).__name__)
        return result
    prior = defaultdict(dict)
    prior_duplicates = {"duplicates": 0, "conflicts": 0}
    try:
        for file in baselines:
            old_records = core.read_json(file)
            if not isinstance(old_records, list) or not old_records:
                raise core.RefreshError("Ranking baseline must be a nonempty array")
            metrics = {"duplicates": 0, "conflicts": 0}
            for group, people in _ranking_groups(old_records, provider, categories, metrics).items():
                prior[group].update(people)
            for field in prior_duplicates:
                prior_duplicates[field] = max(prior_duplicates[field], metrics[field])
    except (ValueError, TypeError, core.RefreshError) as exc:
        issue("BASELINE_INVALID", errorType=type(exc).__name__)
        return result
    result["candidate"] = {"records": len(records), "groups": len(current),
                           "duplicateRecognitionRows": duplicates["duplicates"], "conflictingDuplicateRows": duplicates["conflicts"],
                           "editions": sorted({str(key[2]) if key[2] is not None else "unknown" for key in current})}
    result["baseline"] = {"uniqueRecognitionRecords": sum(len(group) for group in prior.values()), "groups": len(prior)}
    if duplicates["duplicates"] > prior_duplicates["duplicates"]:
        issue("RANKING_DUPLICATES_EXPANDED", count=duplicates["duplicates"] - prior_duplicates["duplicates"])
    if duplicates["conflicts"] > prior_duplicates["conflicts"]:
        issue("RANKING_CONFLICTING_DUPLICATES_EXPANDED", count=duplicates["conflicts"] - prior_duplicates["conflicts"])
    if not prior:
        result["warnings"].append("Initial ranking snapshot: no historical coverage comparison is possible")
    target_categories = set(categories) if categories else {key[0] for key in prior}
    missing_categories = target_categories - {key[0] for key in current}
    if missing_categories:
        issue("RANKING_CATEGORY_COVERAGE_LOST", count=len(missing_categories))
    comparisons = []
    for category in sorted({key[0] for key in current}):
        for year in sorted({key[2] for key in current if key[0] == category}, key=lambda value: -1 if value is None else value):
            previous_years = {key[2] for key in prior if key[0] == category and (key[2] is None or year is None or key[2] <= year)}
            if year in previous_years:
                previous = year
            else:
                known = [value for value in previous_years if value is not None]
                previous = max(known) if known else None
            before_scopes = {key[1] for key in prior if key[0] == category and key[2] == previous}
            after_scopes = {key[1] for key in current if key[0] == category and key[2] == year}
            if before_scopes - after_scopes:
                issue("RANKING_GEOGRAPHY_COVERAGE_LOST", category=category, count=len(before_scopes - after_scopes))
            if year is None and any(value is not None for value in previous_years):
                issue("RANKING_EDITION_UNDECLARED", category=category)
            for scope in sorted(after_scopes & before_scopes):
                old, new = prior[(category, scope, previous)], current[(category, scope, year)]
                same_edition = year == previous
                item = {"category": category, "scope": scope, "edition": year, "previousEdition": previous,
                        "before": len(old), "after": len(new), "sameEdition": same_edition}
                if len(new) < len(old) * .8:
                    issue("RANKING_SCOPE_COVERAGE_LOST", category=category, before=len(old), after=len(new))
                if same_edition:
                    removed = len(set(old) - set(new))
                    item["removedRecognitionRecords"] = removed
                    if removed > len(old) * .20:
                        issue("RANKING_SAME_EDITION_PEOPLE_LOST", category=category, count=removed)
                for field in ("rank", "firm", "city", "crd" if provider == "barrons" else "uri"):
                    before = sum(normalized(row.get(field)) not in EMPTY for row in old.values())
                    after = sum(normalized(row.get(field)) not in EMPTY for row in new.values())
                    if before and after / len(new) < (before / len(old)) * .8:
                        issue("RANKING_FIELD_COVERAGE_LOST", field=field, before=before, after=after)
                comparisons.append(item)
    result["cohortComparisons"] = comparisons
    result["status"] = "held" if result["issues"] else "accepted"
    return result


def compare_source(stage, sid, candidate, options=None):
    paths = baseline_paths(stage, sid)
    before = core.facts(paths, stage)
    candidate_hash = core.file_hash(candidate)
    if sid.startswith("roster:"):
        result = compare_roster(candidate, paths[0] if paths else None, sid[7:])
    elif sid in {"barrons", "forbes"}:
        result = compare_rankings(candidate, paths, sid, (options or {}).get("categories", ()))
        result["baselineSha256"] = core.digest(before) if before else None
    elif sid in {"sec_individuals", "sec_firms", "sec_adv"}:
        from refresh_sec_history import compare
        result = compare(candidate, paths[0] if paths else None, sid, options)
    else:
        raise core.RefreshError("Source historical policy is not implemented")
    if (core.facts(baseline_paths(stage, sid), stage) != before or core.file_hash(candidate) != candidate_hash
            or result["candidateSha256"] != candidate_hash):
        raise core.RefreshError("Historical source inputs changed during assessment")
    result["baselineFacts"] = before
    return result
