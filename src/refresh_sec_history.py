"""SEC native-CRD continuity and field checks over complete source archives.

Streaming XML retains compact per-CRD fingerprints, never the full feed tree.
Historical differences are observations, not confirmed employment departures.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
import csv
import gzip
import hashlib
import io
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib

import refresh_core as core

MAX_MEMBER_BYTES = 2 * 1024 ** 3
MAX_ARCHIVE_BYTES = 8 * 1024 ** 3
NUMBER = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
EMPTY = {"", "n/a", "na", "none", "nan"}


def _norm(value):
    return " ".join(str(value or "").split()).casefold()


def _fingerprint(value):
    return hashlib.sha256(repr(value).encode("utf-8")).digest()[:16]


def _number(value):
    # Match adv._num: commas and outer padding are accepted, dollar signs
    # and internal spaces are not. Do not normalize more than the consumer.
    text = str(value or "").replace(",", "").strip()
    if not NUMBER.fullmatch(text): return None
    try: result = Decimal(text)
    except InvalidOperation: return None
    return result if result.is_finite() else None


class XmlReader:
    def __init__(self, stream, budget):
        self.stream, self.budget, self.size, self.tail = stream, budget, 0, b""

    def read(self, size=-1):
        chunk = self.stream.read(size)
        self.size += len(chunk); self.budget[0] += len(chunk)
        if self.size > MAX_MEMBER_BYTES or self.budget[0] > MAX_ARCHIVE_BYTES:
            raise ValueError("SEC archive exceeds uncompressed size limit")
        text = (self.tail + chunk).upper()
        if b"\x00" in chunk:
            raise ValueError("SEC XML non-UTF8/NUL encoding refused")
        if b"<!DOCTYPE" in text or b"<!ENTITY" in text:
            raise ValueError("SEC XML DTD/entity declarations refused")
        self.tail = text[-16:]
        return chunk


def _xml_record(element, individual):
    info = element.find("Info")
    if info is None:
        raise ValueError("SEC record lacks expected Info schema")
    ident = info.get("indvlPK" if individual else "FirmCrdNb", "")
    if not re.fullmatch(r"[0-9]+", ident) or int(ident) <= 0:
        raise ValueError("SEC record lacks a valid native CRD")
    if individual:
        primary = tuple(_norm(info.get(field)) for field in ("firstNm", "midNm", "lastNm", "sufNm"))
        aliases = sorted(tuple(_norm(alias.get(field)) for field in ("firstNm", "midNm", "lastNm", "sufNm"))
                         for alias in element.findall("./OthrNms/OthrNm"))
        employers = element.findall("./CrntEmps/CrntEmp")
        firms = sorted({emp.get("orgPK") for emp in employers if emp.get("orgPK")})
        addresses = employers + element.findall("./CrntEmps/CrntEmp/BrnchOfLocs/BrnchOfLoc")
        registration_count = len(element.findall("./CrntEmps/CrntEmp/CrntRgstns/CrntRgstn"))
        extras = {"otherNameRecords": bool(aliases), "otherNameForms": len(aliases),
                  "employmentRecords": bool(firms), "employmentLinks": len(firms), "registrationRows": registration_count,
                  "employmentHistoryRows": len(element.findall("./EmpHss/EmpHs")),
                  "examRows": len(element.findall("./Exms/Exm")), "designationRows": len(element.findall("./Dsgntns/Dsgntn")),
                  "priorRegistrationRows": len(element.findall("./PrevRgstns/PrevRgstn")),
                  "disclosureRows": len(element.findall("./DRPs/DRP")), "otherBusinessRows": len(element.findall("./OthrBuss/OthrBus"))}
        names = (primary, aliases)
        named = bool(primary[0] or primary[2])
    else:
        names = tuple(_norm(info.get(field)) for field in ("BusNm", "LegalNm"))
        named = any(names)
        firms, addresses = [], element.findall("MainAddr")
        extras = {"registrationRows": len(element.findall("./NoticeFiled/States")),
                  "websiteRows": len(element.findall("./FormInfo/Part1A/Item1/WebAddrs/WebAddr"))}
    address_fields = ("str1", "city", "state", "postlCd", "cntry", "str2") if individual else ("Strt1", "City", "State", "PostlCd", "Cntry", "Strt2")
    locations = sorted({tuple(_norm(address.get(field)) for field in address_fields)
                        for address in addresses})
    locations = [value for value in locations if any(value)]
    metrics = {"nameRecords": named, "locationRecords": bool(locations), "locationRows": len(locations),
               "streetRecords": any(row[0] for row in locations), "cityRecords": any(row[1] for row in locations),
               "stateRecords": any(row[2] for row in locations), **extras}
    return ident, (_fingerprint(names), _fingerprint(firms), _fingerprint(locations)), metrics


def _format_reason(exc):
    fixed = {
        "SEC record lacks expected Info schema": "INFO_SCHEMA_MISSING",
        "SEC record lacks a valid native CRD": "NATIVE_CRD_INVALID",
        "Duplicate SEC native CRD": "NATIVE_CRD_DUPLICATED",
        "Duplicate SEC archive member names": "ARCHIVE_MEMBER_DUPLICATED",
        "Namespaced SEC schema is unsupported by downstream parsers": "UNSUPPORTED_XML_NAMESPACE",
        "SEC XML DTD/entity declarations refused": "XML_DTD_REFUSED",
        "SEC XML non-UTF8/NUL encoding refused": "XML_ENCODING_UNSUPPORTED",
        "SEC feed contains no expected records": "EXPECTED_RECORDS_MISSING",
        "SEC ADV has invalid or duplicate native CRD": "ADV_NATIVE_CRD_INVALID_OR_DUPLICATE",
        "SEC ADV required schema is missing or duplicated": "ADV_COLUMNS_INVALID",
        "SEC ADV row is truncated": "ADV_ROW_TRUNCATED",
        "SEC ADV RAUM is not numeric": "ADV_ASSET_VALUE_INVALID",
        "SEC ADV RAUM is not a nonnegative finite value": "ADV_ASSET_VALUE_INVALID",
    }
    if str(exc) in fixed: return fixed[str(exc)]
    if isinstance(exc, ET.ParseError): return "XML_PARSE_ERROR"
    if isinstance(exc, EOFError): return "ARCHIVE_TRUNCATED"
    if isinstance(exc, (zipfile.BadZipFile, gzip.BadGzipFile, zlib.error)): return "COMPRESSED_STREAM_INVALID"
    if isinstance(exc, OSError): return "SOURCE_READ_ERROR"
    return "SOURCE_FORMAT_OR_SIZE_INVALID"


def xml_summary(path, individual):
    people, coverage, record_count = {}, Counter(), 0
    budget, parts = [0], 0
    def consume(stream):
        nonlocal record_count
        stack = []
        for event, element in ET.iterparse(XmlReader(stream, budget), events=("start", "end")):
            if event == "start":
                stack.append(element); continue
            local = element.tag.rsplit("}", 1)[-1]
            if local == ("Indvl" if individual else "Firm"):
                if element.tag != local:
                    raise ValueError("Namespaced SEC schema is unsupported by downstream parsers")
                ident, fingerprints, metrics = _xml_record(element, individual)
                if ident in people:
                    raise ValueError("Duplicate SEC native CRD")
                people[ident] = fingerprints
                coverage.update(metrics); record_count += 1
                if len(stack) > 1: stack[-2].remove(element)
                element.clear()
            stack.pop()
    if individual:
        with zipfile.ZipFile(path) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len({item.filename for item in members}) != len(members):
                raise ValueError("Duplicate SEC archive member names")
            for item in members:
                if item.file_size > MAX_MEMBER_BYTES:
                    raise ValueError("SEC archive member exceeds size limit")
                with archive.open(item) as stream:
                    if item.filename.lower().endswith(".xml"):
                        parts += 1; consume(stream)
                    else:
                        # Fully read auxiliary members too, so their CRC is
                        # checked without extracting any path to the filesystem.
                        while chunk := stream.read(1024 * 1024):
                            budget[0] += len(chunk)
                            if budget[0] > MAX_ARCHIVE_BYTES:
                                raise ValueError("SEC archive exceeds size limit")
    else:
        with gzip.open(path, "rb") as stream:
            parts = 1; consume(stream)
    if not record_count:
        raise ValueError("SEC feed contains no expected records")
    return people, {"records": record_count, "archiveXmlParts": parts, "fields": dict(coverage)}


def adv_summary(path, source_format=None):
    from adv import COLS, NUMERIC_FIELDS, FLAG_FIELDS, CLIENT_TYPES, ALLOC
    people, coverage, assets, rows = {}, Counter(), Decimal(0), 0
    observed_fields = []
    numeric_columns = {key: COLS[key] for key in NUMERIC_FIELDS}
    numeric_columns.update({"client_count_" + letter: "5D(" + letter + ")(1)" for letter in CLIENT_TYPES})
    numeric_columns.update({"client_raum_" + letter: "5D(" + letter + ")(3)" for letter in CLIENT_TYPES})
    numeric_columns.update({"allocation_" + block + "_" + roman: "5.K.(1)(" + block + ")(" + roman + ") end year percentage"
                            for roman in ALLOC for block in ("a", "b")})
    def consume(stream):
        nonlocal rows, assets
        reader = csv.DictReader(stream, strict=True)
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)) or not set(COLS.values()).issubset(fields):
            raise ValueError("SEC ADV required schema is missing or duplicated")
        observed_fields.extend(fields)
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("SEC ADV row is truncated")
            ident = str(row.get(COLS["crd"]) or "").strip()
            if not re.fullmatch(r"[0-9]+", ident) or int(ident) <= 0 or ident in people:
                raise ValueError("SEC ADV has invalid or duplicate native CRD")
            names = tuple(_norm(row.get(COLS[key])) for key in ("name", "legal_name"))
            location = tuple(_norm(row.get(COLS[key])) for key in ("city", "state", "postal"))
            amount = _norm(row.get(COLS["raum_total"]))
            populated = amount not in EMPTY
            if populated:
                value = _number(row.get(COLS["raum_total"]))
                if value is None:
                    raise ValueError("SEC ADV RAUM is not numeric")
                if value < 0:
                    raise ValueError("SEC ADV RAUM is not a nonnegative finite value")
                assets += value
            people[ident] = (_fingerprint(names), _fingerprint(()), _fingerprint(location))
            coverage.update({"nameRecords": any(names), "cityRecords": bool(location[0]), "stateRecords": bool(location[1]),
                             "locationRecords": any(location), "assetRecords": populated,
                             "phoneRecords": bool(_norm(row.get(COLS["phone"]))), "websiteRecords": bool(_norm(row.get(COLS["website"])))})
            # Every column actually consumed by the ADV loader gets its own
            # completeness metric; a stable RAUM total must not hide lost
            # account counts, business activity or product-fit fields.
            for field, column in COLS.items():
                value = _norm(row.get(column))
                coverage["adv:" + field] += value not in EMPTY
                if field in FLAG_FIELDS:
                    coverage["advYes:" + field] += value == "y"
            for field, column in numeric_columns.items():
                value = _number(row.get(column))
                coverage["advNumeric:" + field] += value is not None
                coverage["advPositive:" + field] += value is not None and value > 0
            # The consumer preserves unmapped raw columns too. Retain their
            # completeness so CIK, affiliation, custody and future-use fields
            # cannot disappear behind stable curated assets/name metrics.
            for column, value in row.items():
                coverage["raw:" + column] += _norm(value) not in EMPTY
            rows += 1
    if (source_format or Path(path).suffix.lower().lstrip(".")) == "zip":
        with zipfile.ZipFile(path) as archive:
            entries = [item for item in archive.infolist() if not item.is_dir()]
            if any(item.file_size > MAX_MEMBER_BYTES for item in entries) or sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES:
                raise ValueError("SEC ADV archive exceeds uncompressed size limit")
            if archive.testzip(): raise ValueError("SEC ADV ZIP CRC failed")
            members = [item for item in archive.infolist() if item.filename.lower().endswith(".csv")]
            if len(members) != 1 or members[0].file_size > MAX_MEMBER_BYTES:
                raise ValueError("SEC ADV ZIP must contain one bounded CSV")
            with archive.open(members[0]) as stream:
                consume(io.TextIOWrapper(stream, encoding="latin-1", newline=""))
    else:
        if Path(path).stat().st_size > MAX_MEMBER_BYTES:
            raise ValueError("SEC ADV CSV exceeds size limit")
        with Path(path).open(encoding="latin-1", newline="") as stream: consume(stream)
    if not rows: raise ValueError("SEC ADV contains no firm records")
    return people, {"records": rows, "archiveXmlParts": 0, "fields": dict(coverage),
                    "columns": sorted(observed_fields), "reportedRaumTotal": str(assets)}


def compare(candidate, baseline, sid, options=None):
    candidate_hash = core.file_hash(candidate)
    baseline_hash = core.file_hash(baseline) if baseline else None
    result = {"schemaVersion": 1, "policyVersion": "sec-history-v1:" + sid, "sourceId": sid,
              "candidateSha256": candidate_hash, "baselineSha256": baseline_hash,
              "comparisonOptions": options or {}, "status": "held", "issues": [], "warnings": [],
              "thresholds": {"maxMissingCrdFraction": .02, "maxAddedCrdFraction": .25,
                             "maxFieldDensityLossFraction": .05, "maxFingerprintChangeFraction": .20}}
    def issue(code, **facts): result["issues"].append({"code": code, **facts})
    def load(path, current=False):
        return adv_summary(path, (options or {}).get("format") if current else None) if sid == "sec_adv" else xml_summary(path, sid == "sec_individuals")
    failures = (ValueError, OSError, EOFError, csv.Error, ET.ParseError, zipfile.BadZipFile, zlib.error)
    try:
        current, summary = load(candidate, current=True)
        result["candidate"] = summary
    except failures as exc:
        issue("SEC_FORMAT_INVALID", errorType=type(exc).__name__, reasonCode=_format_reason(exc))
        return result
    if not summary["fields"].get("nameRecords"):
        issue("SEC_REQUIRED_NAMES_MISSING")
    if not baseline:
        result["warnings"].append("Initial SEC snapshot: no historical CRD/field comparison is possible")
        result["status"] = "held" if result["issues"] else "accepted"
        return result
    try:
        old, old_summary = (current, summary) if candidate_hash == baseline_hash else load(baseline)
        result["baseline"] = old_summary
    except failures as exc:
        issue("BASELINE_INVALID", errorType=type(exc).__name__, reasonCode=_format_reason(exc))
        return result
    common = set(old) & set(current)
    removed, added = len(set(old) - set(current)), len(set(current) - set(old))
    result["continuity"] = {"retained": len(common), "removed": removed, "added": added}
    if removed > len(old) * .02: issue("SEC_CRDS_DISAPPEARED", count=removed)
    if added > max(1, len(old) * .25): issue("SEC_UNEXPECTED_CRD_GROWTH", count=added)
    if summary["archiveXmlParts"] != old_summary["archiveXmlParts"]:
        issue("SEC_ARCHIVE_PART_COUNT_CHANGED", before=old_summary["archiveXmlParts"], after=summary["archiveXmlParts"])
    for field, before in old_summary["fields"].items():
        after = summary["fields"].get(field, 0)
        minimum = 25 if field.startswith(("raw:", "advPositive:", "advYes:")) else 1
        if before >= minimum and after / len(current) < before / len(old) * .95:
            issue("SEC_FIELD_COVERAGE_LOST", field=field, before=before, after=after)
    if sid == "sec_adv":
        missing_columns = set(old_summary.get("columns", [])) - set(summary.get("columns", []))
        if missing_columns: issue("SEC_ADV_COLUMNS_DISAPPEARED", count=len(missing_columns))
    result["changedFingerprints"] = {}
    for number, field in enumerate(("names", "employments", "locations")):
        changed = sum(old[key][number] != current[key][number] for key in common)
        result["changedFingerprints"][field] = changed
        if changed > len(common) * .20: issue("SEC_RECORD_VALUES_CHANGED", field=field, count=changed)
    if sid == "sec_adv":
        before, after = Decimal(old_summary["reportedRaumTotal"]), Decimal(summary["reportedRaumTotal"])
        if before and abs(after - before) / before > Decimal("0.5"):
            issue("SEC_AGGREGATE_ASSETS_CHANGED", before=str(before), after=str(after))
    result["status"] = "held" if result["issues"] else "accepted"
    return result
