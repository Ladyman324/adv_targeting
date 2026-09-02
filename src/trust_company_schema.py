"""Build a provenance-bound, non-production trust-company research report.

The tool stops at institution discovery. It never writes map assets, API
lookups, CRD mappings, contacts, or outbound-email eligibility.
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import io
import json
import pathlib
import re
import sys
import zipfile
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "research" / "trust_companies" / "latest"
SCHEMA_VERSION = 1
TRUST_NAME = re.compile(r"\bTRUST(?:S| COMPANY| COMPANIES)?\b", re.I)
NON_ALNUM = re.compile(r"[^a-z0-9]+")
CORPORATE_WORDS = {"a", "an", "and", "company", "co", "corporation", "corp",
                   "inc", "incorporated", "llc", "lp", "ltd", "na", "national",
                   "association", "the"}
STRONG_IDS = ("rssd", "cik", "lei", "ein", "fdic_cert", "occ_charter",
              "state_charter", "form13f_file")
OFFICIAL_KINDS = {"ffiec_nic", "occ_trust_bank", "regulator_registry"}
SOURCE_PRIORITY = {
    "regulator_registry": 0,
    "occ_trust_bank": 1,
    "ffiec_nic": 2,
    "sec_13f": 3,
}


class ResearchInputError(ValueError):
    """Input cannot be interpreted safely."""


@dataclass
class Record:
    source_kind: str = ""
    source_name: str = ""
    source_record_id: str = ""
    source_locator: str = ""
    source_url: str = ""
    source_snapshot_date: str = ""
    retrieved_utc: str = ""
    as_of_date: str = ""
    legal_name: str = ""
    normalized_name: str = ""
    institution_type: str = ""
    type_evidence: str = ""
    status: str = ""
    status_raw: str = ""
    regulator: str = ""
    charter_authority: str = ""
    state: str = ""
    city: str = ""
    address1: str = ""
    address2: str = ""
    postal_code: str = ""
    website: str = ""
    domain: str = ""
    license_scope: str = ""
    public_private: str = ""
    rssd: str = ""
    cik: str = ""
    lei: str = ""
    ein: str = ""
    fdic_cert: str = ""
    occ_charter: str = ""
    state_charter: str = ""
    form13f_file: str = ""
    crd: str = ""
    sec_file: str = ""
    report_date: str = ""
    filing_date: str = ""
    reportable_13f_value: str = ""
    candidate_reason: str = ""
    notes: str = ""

    def finish(self) -> "Record":
        for field in fields(self):
            setattr(self, field.name, clean(getattr(self, field.name)))
        self.normalized_name = normalize_name(self.legal_name)
        self.cik = digits(self.cik).zfill(10) if digits(self.cik) else ""
        self.lei = self.lei.upper()
        for name in ("ein", "rssd", "fdic_cert", "occ_charter", "crd"):
            setattr(self, name, digits(getattr(self, name)))
        self.form13f_file = normalize_13f(self.form13f_file)
        self.state = self.state.upper()
        self.domain = normalize_domain(self.domain or self.website)
        if self.state_charter and ":" not in self.state_charter and self.state:
            self.state_charter = f"{self.state}:{self.state_charter}"
        return self


def clean(value: object) -> str:
    text = "" if value is None else str(value).strip()
    return "" if text.lower() in {"nan", "none", "null"} else text


def digits(value: object) -> str:
    return re.sub(r"\D", "", clean(value))


def normalize_13f(value: object) -> str:
    text = clean(value).upper().replace(" ", "")
    if not text:
        return ""
    match = re.fullmatch(r"028-?(\d+)", text)
    if match:
        return f"028-{match.group(1)}"
    number = digits(text)
    return f"028-{number}" if number else ""


def normalize_name(value: object) -> str:
    return " ".join(token for token in NON_ALNUM.sub(" ", clean(value).lower()).split()
                    if token not in CORPORATE_WORDS)


def normalize_domain(value: object) -> str:
    text = re.sub(r"^[a-z]+://", "", clean(value).lower()).split("/", 1)[0]
    text = text.split("@")[-1].split(":", 1)[0].strip(".")
    return text[4:] if text.startswith("www.") else text


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decode(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ResearchInputError("input is not UTF-8 or Windows-1252 text")


def dict_rows(text: str) -> list[dict[str, str]]:
    try:
        delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",|\t").delimiter
    except csv.Error:
        delimiter = "\t" if "\t" in text else "|" if "|" in text else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ResearchInputError("tabular input has no header")
    return [{clean(key).upper(): clean(value) for key, value in row.items()
             if key is not None} for row in reader]


def member_bytes(path: pathlib.Path, *names: str) -> tuple[str, bytes]:
    wanted = {name.upper() for name in names}
    if not zipfile.is_zipfile(path):
        return path.name, path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        matches = [name for name in archive.namelist()
                   if pathlib.PurePosixPath(name).name.upper() in wanted]
        if len(matches) != 1:
            raise ResearchInputError(
                f"{path} has {len(matches)} matching members; expected one of {sorted(wanted)}")
        return matches[0], archive.read(matches[0])
