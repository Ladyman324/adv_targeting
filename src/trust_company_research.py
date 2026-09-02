"""Build a provenance-bound, research-only trust-company comparison report."""
from __future__ import annotations

import argparse
import collections
import contextlib
import csv
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
import zipfile
from dataclasses import asdict, fields
from datetime import datetime, timezone

from trust_company_matching import (
    cluster_records, institution_rows, pair_links, record_key,
)
from trust_company_schema import (
    DEFAULT_OUTPUT, ROOT, SCHEMA_VERSION, Record, ResearchInputError, decode,
    dict_rows, digits, normalize_name, sha256_file,
)
from trust_company_sources import load_ffiec, load_occ, load_registry, load_sec_13f


def load_known_cases(path: pathlib.Path | None) -> list[dict[str, str]]:
    if path is None:
        return []
    rows = dict_rows(decode(path.read_bytes()))
    required = {"CASE_ID", "EXPECTED_NAME"}
    if rows and not required.issubset(rows[0]):
        raise ResearchInputError(f"{path} is missing {sorted(required - set(rows[0]))}")
    return rows


def evaluate_cases(cases: list[dict[str, str]], records: list[Record],
                   institutions: list[dict[str, object]]) -> list[dict[str, object]]:
    by_record = {record_key(record): record for record in records}
    output = []
    for case in cases:
        id_type = case.get("IDENTIFIER_TYPE", "").lower().strip()
        id_value = case.get("IDENTIFIER_VALUE", "").strip()
        if id_type == "cik" and id_value:
            id_value = digits(id_value).zfill(10)
        expected_name = normalize_name(case.get("EXPECTED_NAME", ""))
        expected_state = case.get("EXPECTED_STATE", "").upper().strip()
        matches = []
        for row in institutions:
            members = [by_record[key] for key in str(row["source_records"]).split("|")]
            id_match = bool(id_type and id_value and any(
                getattr(record, id_type, "") == id_value for record in members))
            name_match = normalize_name(row["legal_name"]) == expected_name
            state_match = not expected_state or any(r.state == expected_state for r in members)
            if (id_match if id_type and id_value else name_match and state_match):
                matches.append((row, members))
        required_source = case.get("REQUIRED_SOURCE_KIND", "").strip()
        source_ok = (any(required_source in {r.source_kind for r in members}
                         for _, members in matches) if required_source else bool(matches))
        expected_status = case.get("EXPECTED_REGULATORY_STATUS", "").strip()
        status_ok = any(row["regulatory_status"] == expected_status for row, _ in matches)
        passed = len(matches) == 1 and source_ok and (not expected_status or status_ok)
        output.append({
            "case_id": case.get("CASE_ID", ""), "expected_name": case.get("EXPECTED_NAME", ""),
            "matches": len(matches),
            "research_ids": "|".join(str(row["research_id"]) for row, _ in matches),
            "required_source_kind": required_source,
            "expected_regulatory_status": expected_status,
            "pass": passed, "result": "pass" if passed else "manual_review_or_missing",
        })
    return output


FORMULA_PREFIX = re.compile(r"^\s*[=+\-@]")


def excel_safe(value: object) -> object:
    """Prevent generated CSV cells from being evaluated as Excel formulas."""
    if isinstance(value, str) and FORMULA_PREFIX.match(value):
        return "'" + value
    return value


@contextlib.contextmanager
def report_build_lock(output: pathlib.Path):
    """Take an OS-backed one-byte lock; it is released automatically on exit/crash."""
    output.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output.parent / f".{output.name}.build.lock"
    handle = lock_path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - production operator is Windows
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise ResearchInputError(
            f"another trust-company report build is already publishing to {output}") from exc
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def atomic_write_text(path: pathlib.Path, text: str) -> None:
    """Publish one complete file in-place; callers publish the manifest last."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                     dir=path.parent)
    temp_path = pathlib.Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def write_csv(path: pathlib.Path, rows: list[dict[str, object]],
              fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                     dir=path.parent)
    temp_path = pathlib.Path(temporary)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows({key: excel_safe(value) for key, value in row.items()}
                             for row in rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def descriptor(kind: str, path: pathlib.Path, as_of_date: str = "",
               retrieved_utc: str = "") -> dict[str, object]:
    return {"kind": kind, "path": str(path.resolve()), "sha256": sha256_file(path),
            "bytes": path.stat().st_size, "asOfDate": as_of_date,
            "retrievedUtc": retrieved_utc}


def _build_report_unlocked(ffiec: pathlib.Path | None, sec_13f: pathlib.Path | None,
                           registries: list[pathlib.Path],
                           known_cases: pathlib.Path | None, output: pathlib.Path,
                           occ: pathlib.Path | None = None,
                           source_dates: dict[str, str] | None = None) -> dict[str, object]:
    records: list[Record] = []
    inputs: list[dict[str, object]] = []
    source_dates = source_dates or {}
    retrieved_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if ffiec:
        date = source_dates.get("ffiec_nic", "")
        records.extend(load_ffiec(ffiec, date, retrieved_utc))
        inputs.append(descriptor("ffiec_nic", ffiec, date, retrieved_utc))
    if occ:
        date = source_dates.get("occ_trust_bank", "")
        occ_records = load_occ(occ, date, retrieved_utc)
        records.extend(occ_records)
        date = date or occ_records[0].source_snapshot_date
        inputs.append(descriptor("occ_trust_bank", occ, date, retrieved_utc))
    if sec_13f:
        date = source_dates.get("sec_13f", "")
        records.extend(load_sec_13f(sec_13f, date, retrieved_utc))
        inputs.append(descriptor("sec_13f", sec_13f, date, retrieved_utc))
    for path in registries:
        date = source_dates.get("regulator_registry", "")
        records.extend(load_registry(path, date, retrieved_utc))
        inputs.append(descriptor("regulator_registry", path, date, retrieved_utc))
    if known_cases:
        inputs.append(descriptor("known_cases", known_cases, retrieved_utc=retrieved_utc))
    if not records:
        raise ResearchInputError("no trust-company source records were loaded")
    duplicates = [key for key, count in collections.Counter(
        record_key(record) for record in records).items() if count > 1]
    if duplicates:
        raise ResearchInputError("duplicate source record keys: " + ", ".join(duplicates[:10]))

    records.sort(key=record_key)
    links = pair_links(records)
    institutions = institution_rows(cluster_records(records, links))
    regulated = [row for row in institutions
                 if row["regulatory_status"] == "verified_trust_company"]
    prospects = [row for row in institutions
                 if row["prospect_status"] in {"candidate", "priority_candidate"}]
    cases = evaluate_cases(load_known_cases(known_cases), records, institutions)
    linked = {link[side] for link in links if link["action"] == "auto_link"
              for side in ("left_record", "right_record")}
    unresolved = [asdict(record) for record in records if record_key(record) not in linked]
    by_source = dict(sorted(collections.Counter(r.source_name for r in records).items()))
    counts = {
        "sourceRecords": len(records), "institutions": len(institutions),
        "verifiedTrustCompanies": sum(r["regulatory_status"] == "verified_trust_company"
                                       for r in institutions),
        "candidateOnly": sum(r["regulatory_status"] == "candidate_only" for r in institutions),
        "historicalOrInactive": sum(
            r["regulatory_status"] == "historical_or_inactive" for r in institutions),
        "autoLinks": sum(r["action"] == "auto_link" for r in links),
        "reviewLinks": sum(r["action"] == "review" for r in links),
        "blockedConflicts": sum(r["action"] == "block_conflict" for r in links),
        "unresolvedRecords": len(unresolved), "knownCases": len(cases),
        "knownCaseFailures": sum(not r["pass"] for r in cases),
        "regulatedUniverse": len(regulated), "prospectCandidates": len(prospects),
        "excludedFromProspects": len(institutions) - len(prospects),
    }
    summary = {
        "schemaVersion": SCHEMA_VERSION, "mode": "research_only",
        "productionEligible": False, "inputs": inputs, "counts": counts,
        "recordsBySource": by_source,
        "recordsBySourceKind": dict(sorted(collections.Counter(
            r.source_kind for r in records).items())),
        "institutionsByType": dict(sorted(collections.Counter(
            str(r["institution_type"]) for r in institutions).items())),
        "prospectsByBucket": dict(sorted(collections.Counter(
            str(r["prospect_bucket"]) for r in institutions).items())),
        "safety": {"noProductionOutput": True, "crdNeverUsedAsMergeKey": True,
                   "nameOrDomainNeverAutoMerges": True,
                   "strongIdConflictsFailClosed": True},
    }
    output.mkdir(parents=True, exist_ok=True)
    record_fields = [field.name for field in fields(Record)]
    write_csv(output / "source_records.csv", [asdict(r) for r in records], record_fields)
    write_csv(output / "institutions.csv", institutions, list(institutions[0]))
    write_csv(output / "regulated_universe.csv", regulated, list(institutions[0]))
    write_csv(output / "prospect_candidates.csv", prospects, list(institutions[0]))
    link_fields = ["left_record", "right_record", "left_name", "right_name",
                   "action", "confidence", "evidence", "conflicts"]
    write_csv(output / "proposed_links.csv", links, link_fields)
    write_csv(output / "unresolved_records.csv", unresolved, record_fields)
    case_fields = ["case_id", "expected_name", "matches", "research_ids",
                   "required_source_kind", "expected_regulatory_status", "pass", "result"]
    write_csv(output / "known_case_results.csv", cases, case_fields)
    atomic_write_text(output / "summary.json",
                      json.dumps(summary, indent=2, sort_keys=True) + "\n")
    lines = ["# Trust-company research summary", "",
             "Research-only: not eligible for the production map or email system.", "",
             f"- Source records: {counts['sourceRecords']:,}",
             f"- Research institutions: {counts['institutions']:,}",
             f"- Regulator-verified: {counts['verifiedTrustCompanies']:,}",
             f"- Candidate-only: {counts['candidateOnly']:,}",
             f"- Historical or inactive: {counts['historicalOrInactive']:,}",
             f"- Strong-ID links: {counts['autoLinks']:,}",
             f"- Review links: {counts['reviewLinks']:,}",
             f"- Blocked conflicts: {counts['blockedConflicts']:,}",
             f"- Known-case failures: {counts['knownCaseFailures']:,}", "",
             f"- Broad regulated universe: {counts['regulatedUniverse']:,}",
             f"- Screened prospect candidates: {counts['prospectCandidates']:,}",
             f"- Excluded from prospect candidates: {counts['excludedFromProspects']:,}", "",
             "## Source coverage", ""]
    lines.extend(f"- {name}: {count:,}" for name, count in by_source.items())
    lines.extend(["", "## Institution types", ""])
    lines.extend(f"- {name}: {count:,}" for name, count in summary["institutionsByType"].items())
    lines.extend(["", "## Prospect screening", ""])
    lines.extend(f"- {name}: {count:,}" for name, count in summary["prospectsByBucket"].items())
    lines.extend(["", "## Safety boundary", ""])
    lines.extend(f"- {name}: PASS" for name in summary["safety"])
    atomic_write_text(output / "summary.md", "\n".join(lines) + "\n")
    generated = ["source_records.csv", "institutions.csv", "regulated_universe.csv",
                 "prospect_candidates.csv", "proposed_links.csv",
                 "unresolved_records.csv", "known_case_results.csv", "summary.json",
                 "summary.md"]
    manifest = {"schemaVersion": SCHEMA_VERSION,
                "builtUtc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "mode": "research_only", "productionEligible": False, "inputs": inputs,
                "outputs": {name: sha256_file(output / name) for name in generated}}
    manifest["manifestHash"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    # The manifest is the commit marker and is therefore always published last.
    atomic_write_text(output / "manifest.json",
                      json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return summary


def build_report(ffiec: pathlib.Path | None, sec_13f: pathlib.Path | None,
                 registries: list[pathlib.Path], known_cases: pathlib.Path | None,
                 output: pathlib.Path, occ: pathlib.Path | None = None,
                 source_dates: dict[str, str] | None = None) -> dict[str, object]:
    with report_build_lock(output):
        return _build_report_unlocked(ffiec, sec_13f, registries, known_cases,
                                      output, occ=occ, source_dates=source_dates)


def repository_relative(path: pathlib.Path) -> pathlib.Path | None:
    """Return a repo-relative path across drive-letter and UNC aliases."""
    candidate = path.resolve()
    tail: list[str] = []
    while True:
        if candidate.exists():
            try:
                if os.path.samefile(candidate, ROOT):
                    return pathlib.Path(*reversed(tail))
            except OSError:
                pass
        if candidate == candidate.parent:
            return None
        tail.append(candidate.name)
        candidate = candidate.parent


def assert_research_output(path: pathlib.Path) -> None:
    relative = repository_relative(path)
    if relative is None:
        return
    parts = tuple(part.lower() for part in relative.parts)
    if parts[:3] != ("data", "research", "trust_companies"):
        raise ResearchInputError(
            "repository output must stay under data/research/trust_companies")


def write_templates(directory: pathlib.Path) -> None:
    assert_research_output(directory)
    directory.mkdir(parents=True, exist_ok=True)
    registry_fields = ["source_name", "source_record_id", "source_url",
        "source_snapshot_date", "as_of_date",
        "legal_name", "institution_type", "type_evidence", "status", "regulator",
        "charter_authority", "state", "city", "address1", "address2", "postal_code",
        "website", "domain", "license_scope", "public_private", "rssd", "cik",
        "lei", "ein", "fdic_cert", "occ_charter",
        "state_charter", "form13f_file", "crd", "sec_file", "notes"]
    case_fields = ["case_id", "expected_name", "expected_state", "identifier_type",
        "identifier_value", "required_source_kind", "expected_regulatory_status", "notes"]
    write_csv(directory / "regulator_registry_template.csv", [], registry_fields)
    write_csv(directory / "known_cases_template.csv", [], case_fields)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--ffiec", type=pathlib.Path)
    build.add_argument("--ffiec-as-of", default="")
    build.add_argument("--occ", type=pathlib.Path)
    build.add_argument("--occ-as-of", default="")
    build.add_argument("--sec-13f", type=pathlib.Path)
    build.add_argument("--sec-13f-as-of", default="")
    build.add_argument("--registry", type=pathlib.Path, action="append", default=[])
    build.add_argument("--known-cases", type=pathlib.Path)
    build.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    init = commands.add_parser("init-templates")
    init.add_argument("--directory", type=pathlib.Path,
                      default=ROOT / "data/research/trust_companies/input")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "init-templates":
            write_templates(args.directory)
            print(f"[+] wrote templates under {args.directory}")
            return 0
        assert_research_output(args.output)
        summary = build_report(
            args.ffiec, args.sec_13f, args.registry, args.known_cases, args.output,
            occ=args.occ, source_dates={"ffiec_nic": args.ffiec_as_of,
                "occ_trust_bank": args.occ_as_of, "sec_13f": args.sec_13f_as_of})
        print(f"[+] wrote research-only report to {args.output}")
        print(f"[*] counts: {summary['counts']}")
        return 2 if summary["counts"]["knownCaseFailures"] else 0
    except (OSError, ResearchInputError, zipfile.BadZipFile, csv.Error) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
