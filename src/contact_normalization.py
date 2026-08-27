"""Pure contact normalization; production uses Act JSON and roster rows only."""
from __future__ import annotations

import re
from dataclasses import dataclass


PRODUCTION_SOURCE_SYSTEMS = frozenset({"act_json", "firm_roster"})
HONORIFICS = frozenset({"mr", "mrs", "ms", "miss", "dr", "prof", "rev", "sir"})
SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v", "jnr", "snr"})
NORMALIZED_EVIDENCE_FIELDS = (
    "source_system", "source_file", "source_person_id", "source_branch_id",
    "source_team_id", "source_profile_url", "source_updated_utc",
    "source_full_name", "name_parse_status", "name_first", "name_middle",
    "name_last", "name_suffix",
    "preferred_name", "name", "address_line1", "address_line2", "city",
    "state", "postal", "latitude", "longitude", "email", "email_alt",
    "email_personal", "phone", "mobile", "phone_source_kind",
    "phone_source_file", "company", "job_title", "asserted_crd",
)


def text(value):
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def clean_email(value):
    email = text(value).lower()
    return email if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email) else ""


def normalize_phone(value):
    """Comparable phone plus extension; empty when the value is not dialable."""
    raw = text(value).lower()
    if not raw:
        return ""
    match = re.search(r"(?:ext\.?|extension|x)\s*(\d{1,8})\s*$", raw)
    extension = match.group(1) if match else ""
    base = raw[:match.start()] if match else raw
    digits = re.sub(r"\D", "", base)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if not 7 <= len(digits) <= 15:
        return ""
    return digits + (f"x{extension}" if extension else "")


def compose_name(first="", middle="", last="", suffix=""):
    return " ".join(part for part in map(text, (first, middle, last, suffix))
                    if part)


def _name_token(value):
    return re.sub(r"[^a-z0-9]", "", text(value).lower())


@dataclass(frozen=True)
class NameParts:
    first: str = ""
    middle: str = ""
    last: str = ""
    suffix: str = ""
    status: str = "incomplete"


def parse_full_name(value):
    """Conservatively parse Act's display name for candidate generation.

    This is a recovery path, not an identity decision. It handles leading
    honorifics, ``Last, First Middle`` order and conventional suffixes. It
    declines single-token and otherwise ambiguous values rather than inventing
    a surname.
    """
    raw = text(value)
    if not raw:
        return NameParts()
    cleaned = raw
    while True:
        match = re.match(r"^\s*([^\s,]+)[\s,]+(.*)$", cleaned)
        if not match or _name_token(match.group(1)) not in HONORIFICS:
            break
        cleaned = match.group(2).strip()
    if "," in cleaned:
        left, right = (part.strip() for part in cleaned.split(",", 1))
        # Display names commonly put a suffix after a comma. That is not the
        # ``Last, First`` form and must not turn "Jr." into a first name.
        if left and _name_token(right) in SUFFIXES:
            tokens = left.split()
            suffix = right
        elif left and right and len(right.split()) >= 1:
            right_tokens = right.split()
            if right_tokens and _name_token(right_tokens[-1]) in SUFFIXES:
                suffix = right_tokens.pop()
            else:
                suffix = ""
            tokens = right_tokens + [left]
        else:
            return NameParts()
    else:
        tokens = cleaned.split()
        if tokens and _name_token(tokens[-1]) in SUFFIXES:
            suffix = tokens.pop()
        else:
            suffix = ""
    if len(tokens) < 2:
        return NameParts()
    if _name_token(tokens[0]) in HONORIFICS \
            or _name_token(tokens[-1]) in HONORIFICS:
        return NameParts()
    return NameParts(tokens[0], " ".join(tokens[1:-1]), tokens[-1], suffix,
                     "full_name_parsed")


def resolved_act_name_parts(row):
    """Prefer structured Act names, recovering only demonstrably bad shapes."""
    first = text(row.get("firstName"))
    middle = text(row.get("middleName"))
    last = text(row.get("lastName"))
    suffix = text(row.get("nameSuffix"))
    structured_bad = (
        not first or not last
        or _name_token(first) in HONORIFICS
        or _name_token(last) in HONORIFICS
        or "," in first or "," in last
    )
    if not structured_bad:
        return NameParts(first, middle, last, suffix, "structured")
    recovered = parse_full_name(row.get("fullName"))
    if recovered.first and recovered.last:
        return NameParts(recovered.first, recovered.middle or middle,
                         recovered.last, recovered.suffix or suffix,
                         "full_name_recovered")
    return NameParts(first, middle, last, suffix, "structured_incomplete")


def _get(row, column):
    return text(row.get(column)) if column else ""


def normalize_act_contact(row, source_file=""):
    """One production Act JSON row. Excel-like columns are never consulted."""
    address = row.get("businessAddress") or {}
    if not isinstance(address, dict):
        address = {}
    parts = resolved_act_name_parts(row)
    first, middle, last, suffix = (
        parts.first, parts.middle, parts.last, parts.suffix)
    business_phone = normalize_phone(row.get("businessPhone"))
    extension = re.sub(r"\D", "", text(row.get("businessExtension")))[:8]
    phone = business_phone + (f"x{extension}" if business_phone and extension else "")
    mobile = normalize_phone(row.get("mobilePhone"))
    custom = row.get("customFields") or {}
    if not isinstance(custom, dict):
        custom = {}
    return {
        "source_system": "act_json",
        "source_file": text(source_file),
        "source_person_id": text(row.get("id")),
        "source_branch_id": "",
        "source_team_id": "",
        "source_profile_url": text(row.get("website")),
        "source_updated_utc": text(row.get("edited")),
        "source_full_name": text(row.get("fullName")),
        "name_parse_status": parts.status,
        "name_first": first, "name_middle": middle, "name_last": last,
        "name_suffix": suffix, "preferred_name": text(row.get("salutation")),
        "name": compose_name(first, middle, last, suffix),
        "address_line1": text(address.get("line1")),
        "address_line2": text(address.get("line2")),
        "city": text(address.get("city")),
        "state": text(address.get("state")).upper(),
        "postal": text(address.get("postalCode")),
        "latitude": address.get("latitude"), "longitude": address.get("longitude"),
        "email": clean_email(row.get("emailAddress")),
        "email_alt": clean_email(row.get("altEmailAddress")),
        "email_personal": clean_email(row.get("personalEmailAddress")),
        "phone": phone, "mobile": mobile,
        "phone_source_kind": "act_business_phone" if phone else "",
        "phone_source_file": text(source_file) if phone else "",
        "company": text(row.get("company")),
        "job_title": text(row.get("jobTitle")),
        "asserted_crd": text(custom.get("crd") or custom.get("sec_crd")),
    }


def production_act_rows(rows, source_file=""):
    """The explicit production boundary: only API rows enter this function."""
    return [normalize_act_contact(row, source_file) for row in rows]


def normalize_roster_identity(row, source_system, source_file, columns):
    """Normalize explicit source mappings; never guess among generic id fields."""
    first = _get(row, columns.get("first"))
    middle = _get(row, columns.get("middle"))
    last = _get(row, columns.get("last"))
    suffix = _get(row, columns.get("suffix"))
    published = _get(row, columns.get("name"))
    phone = normalize_phone(row.get(columns.get("phone"))) if columns.get("phone") else ""
    return {
        "source_system": text(source_system),
        "source_file": text(source_file),
        "source_person_id": _get(row, columns.get("person_id")),
        "source_branch_id": _get(row, columns.get("branch_id")),
        "source_team_id": _get(row, columns.get("team_id")),
        "source_profile_url": _get(row, columns.get("profile_url")),
        "source_updated_utc": _get(row, columns.get("updated")),
        "source_full_name": published,
        "name_parse_status": "published" if published else "structured",
        "name_first": first, "name_middle": middle, "name_last": last,
        "name_suffix": suffix,
        "preferred_name": _get(row, columns.get("preferred_name")),
        "name": published or compose_name(first, middle, last, suffix),
        "address_line1": _get(row, columns.get("address1")),
        "address_line2": _get(row, columns.get("address2")),
        "city": _get(row, columns.get("city")),
        "state": _get(row, columns.get("state")).upper(),
        "postal": _get(row, columns.get("postal")),
        "latitude": row.get(columns.get("latitude")) if columns.get("latitude") else None,
        "longitude": row.get(columns.get("longitude")) if columns.get("longitude") else None,
        "email": clean_email(row.get(columns.get("email")))
        if columns.get("email") else "",
        "email_alt": "", "email_personal": "", "phone": phone,
        "mobile": "",
        "phone_source_kind": "firm_roster" if phone else "",
        "phone_source_file": text(source_file) if phone else "",
        "company": _get(row, columns.get("company")),
        "job_title": _get(row, columns.get("job_title")),
        "asserted_crd": _get(row, columns.get("crd")),
    }


def source_key(record):
    return tuple(text(record.get(key)) for key in
                 ("source_system", "source_file", "source_person_id",
                  "source_branch_id"))


@dataclass(frozen=True)
class ExcelPhoneGap:
    """Aggregate diagnostic only: deliberately contains no donor values."""
    act_rows: int
    api_phone_rows: int
    api_missing_phone_rows: int
    excel_phone_rows: int
    uniquely_explained_missing_rows: int
    ambiguous_missing_rows: int
    unexplained_missing_rows: int
    ambiguous_email_count: int


def classify_excel_phone_gap(act_rows, excel_rows, excel_email_field="E-mail",
                             excel_phone_field="Phone"):
    """Measure the migration gap without returning any Excel phone value.

    This function is intentionally disconnected from normalize_act_contact and
    production_act_rows. Its result is counts only and cannot be used as a
    donor mapping.
    """
    act = list(act_rows)
    excel = list(excel_rows)
    by_email = {}
    excel_phone_rows = 0
    for row in excel:
        email = clean_email(row.get(excel_email_field))
        phone = normalize_phone(row.get(excel_phone_field))
        if email and phone:
            excel_phone_rows += 1
            by_email.setdefault(email, set()).add(phone)
    api_phone_rows = sum(bool(normalize_phone(row.get("businessPhone")))
                         for row in act)
    unique = ambiguous = unexplained = 0
    for row in act:
        if normalize_phone(row.get("businessPhone")):
            continue
        phones = by_email.get(clean_email(row.get("emailAddress")), set())
        if len(phones) == 1:
            unique += 1
        elif len(phones) > 1:
            ambiguous += 1
        else:
            unexplained += 1
    return ExcelPhoneGap(
        act_rows=len(act), api_phone_rows=api_phone_rows,
        api_missing_phone_rows=len(act) - api_phone_rows,
        excel_phone_rows=excel_phone_rows,
        uniquely_explained_missing_rows=unique,
        ambiguous_missing_rows=ambiguous,
        unexplained_missing_rows=unexplained,
        ambiguous_email_count=sum(len(phones) > 1 for phones in by_email.values()),
    )
