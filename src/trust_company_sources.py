"""Source adapters for the non-production trust-company research report."""
from __future__ import annotations

import pathlib
import re
import zipfile
from datetime import datetime

from trust_company_schema import (
    Record, ResearchInputError, TRUST_NAME, clean, decode, dict_rows, digits,
    member_bytes, normalize_13f,
)


INACTIVE_STATUS = re.compile(
    r"\b(?:inactive|closed|surrendered|denied|revoked|withdrawn|historical|"
    r"terminated|expired|cancelled|canceled|dissolved)\b", re.I)
NEGATED_CURRENT_STATUS = re.compile(
    r"\b(?:not|no longer)\s+(?:active|current|licensed|open|in good standing)\b|"
    r"\blicen[cs]e\s+(?:is\s+)?not\s+current\b", re.I)
ACTIVE_STATUS = re.compile(
    r"\b(?:active|open|licensed|current|good standing)\b", re.I)


def normalize_registry_status(value: object) -> str:
    """Map regulator wording to current/inactive/unknown without guessing."""
    raw = clean(value)
    if NEGATED_CURRENT_STATUS.search(raw) or INACTIVE_STATUS.search(raw):
        return "inactive"
    if ACTIVE_STATUS.search(raw):
        return "active"
    return "unknown"


def load_ffiec(path: pathlib.Path, snapshot_date: str = "",
               retrieved_utc: str = "") -> list[Record]:
    """Read FFIEC active attributes and retain charter type 250/MTC/NTC."""
    member, raw = member_bytes(path, "CSV_ATTRIBUTES_ACTIVE.CSV",
                               "ATTRIBUTES_ACTIVE.CSV", "ATTRIBUTES-ACTIVE.CSV")
    output = []
    for line, row in enumerate(dict_rows(decode(raw)), start=2):
        entity_type = row.get("ENTITY_TYPE", "").upper()
        charter_type = digits(row.get("CHTR_TYPE_CD"))
        if entity_type not in {"MTC", "NTC"} and charter_type != "250":
            continue
        rssd, name = digits(row.get("ID_RSSD")), row.get("NM_LGL", "")
        if not rssd or not name:
            raise ResearchInputError(f"{path}:{member}:{line} trust row lacks RSSD or name")
        output.append(Record(
            source_kind="ffiec_nic", source_name="FFIEC NIC", source_record_id=rssd,
            source_locator=f"{member}:row:{line}",
            source_url="https://www.ffiec.gov/npw/FinancialReport/DataDownload",
            source_snapshot_date=snapshot_date, retrieved_utc=retrieved_utc,
            as_of_date=row.get("D_DT_START", "") or row.get("DT_START", ""),
            legal_name=name, institution_type="non_depository_trust_company",
            type_evidence=f"ENTITY_TYPE={entity_type};CHTR_TYPE_CD={charter_type}",
            status="active", status_raw="active",
            regulator=row.get("PRIM_FED_REG", ""),
            license_scope="non_deposit_trust_company",
            public_private="not_stated",
            charter_authority={"1": "federal", "2": "state"}.get(
                digits(row.get("CHTR_AUTH_CD")), ""),
            state=row.get("STATE_ABBR_NM", "") or row.get("STATE_INC_ABBR_NM", ""),
            city=row.get("CITY", ""), address1=row.get("STREET_LINE1", ""),
            address2=row.get("STREET_LINE2", ""), postal_code=row.get("ZIP_CD", ""),
            website=row.get("URL", ""), rssd=rssd, lei=row.get("ID_LEI", ""),
            ein=row.get("ID_TAX", ""), fdic_cert=row.get("ID_FDIC_CERT", ""),
            occ_charter=row.get("ID_OCC", "")).finish())
    return output


def load_occ(path: pathlib.Path, snapshot_date: str = "",
             retrieved_utc: str = "") -> list[Record]:
    """Read the OCC's official active trust-bank XLSX workbook safely."""
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - deployment environment issue
        raise ResearchInputError("OCC XLSX input requires pandas and openpyxl") from exc
    try:
        raw = pd.read_excel(path, sheet_name="Trust", header=None, dtype=str)
    except (ImportError, ValueError, OSError) as exc:
        raise ResearchInputError(f"cannot read OCC Trust worksheet: {exc}") from exc
    if raw.empty:
        raise ResearchInputError("OCC Trust worksheet is empty")
    title = " ".join(str(value) for value in raw.iloc[0].dropna().tolist())
    title_match = re.search(r"Active\s+As\s+of\s+(\d{1,2}/\d{1,2}/\d{4})", title, re.I)
    workbook_date = ""
    if title_match:
        workbook_date = datetime.strptime(title_match.group(1), "%m/%d/%Y").date().isoformat()
    if snapshot_date and workbook_date and snapshot_date != workbook_date:
        raise ResearchInputError(
            f"OCC snapshot date mismatch: argument={snapshot_date}; workbook={workbook_date}")
    snapshot_date = snapshot_date or workbook_date

    header_index = None
    aliases = {"CHARTER NO", "NAME", "ADDRESS (LOC)", "CITY", "STATE", "CERT", "RSSD"}
    for index, row in raw.head(20).iterrows():
        values = {str(value).strip().upper() for value in row.dropna().tolist()}
        if aliases.issubset(values):
            header_index = index
            break
    if header_index is None:
        raise ResearchInputError("OCC Trust worksheet lacks the expected seven-column header")
    table = raw.iloc[header_index + 1:].copy()
    table.columns = [str(value).strip().upper() for value in raw.iloc[header_index].tolist()]
    output = []
    for excel_index, row in table.iterrows():
        charter = digits(row.get("CHARTER NO", ""))
        name = str(row.get("NAME", "") or "").strip()
        if not charter or not name or name.lower() == "nan":
            continue
        rssd = digits(row.get("RSSD", ""))
        fdic_cert = digits(row.get("CERT", ""))
        output.append(Record(
            source_kind="occ_trust_bank", source_name="OCC Active Trust Banks",
            source_record_id=charter,
            source_locator=f"Trust:excel-row:{excel_index + 1}",
            source_url=("https://occ.gov/topics/charters-and-licensing/"
                        "financial-institution-lists/trust-by-name.xlsx"),
            source_snapshot_date=snapshot_date, retrieved_utc=retrieved_utc,
            as_of_date=snapshot_date, legal_name=name,
            institution_type="national_trust_bank",
            type_evidence="listed on OCC active Trust Banks worksheet",
            status="active", status_raw="active", regulator="OCC",
            charter_authority="federal",
            state=str(row.get("STATE", "") or ""),
            city=str(row.get("CITY", "") or ""),
            address1=str(row.get("ADDRESS (LOC)", "") or ""),
            license_scope="national_trust_bank", public_private="not_stated",
            rssd="" if rssd == "0" else rssd,
            fdic_cert="" if fdic_cert == "0" else fdic_cert,
            occ_charter=charter,
        ).finish())
    if not output:
        raise ResearchInputError("OCC Trust worksheet contains no institution rows")
    return output


def zip_table(path: pathlib.Path, table: str) -> list[dict[str, str]]:
    _, raw = member_bytes(path, f"{table}.TSV", f"{table}.TXT", f"{table}.CSV")
    return dict_rows(decode(raw))


def date_key(value: str) -> str:
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(value.strip(), fmt).strftime("%Y%m%d")
        except ValueError:
            pass
    return value.strip()


def load_sec_13f(path: pathlib.Path, snapshot_date: str = "",
                 retrieved_utc: str = "") -> list[Record]:
    """Read an official SEC quarterly 13F flat-file archive."""
    if not zipfile.is_zipfile(path):
        raise ResearchInputError("SEC 13F input must be an official quarterly ZIP")
    submissions = {row.get("ACCESSION_NUMBER", ""): row
                   for row in zip_table(path, "SUBMISSION")}
    summaries = {row.get("ACCESSION_NUMBER", ""): row
                 for row in zip_table(path, "SUMMARYPAGE")}
    latest: dict[str, tuple[tuple[str, str, str], Record]] = {}
    for line, row in enumerate(zip_table(path, "COVERPAGE"), start=2):
        accession = row.get("ACCESSION_NUMBER", "")
        submission = submissions.get(accession, {})
        name = row.get("FILINGMANAGER_NAME", "")
        if not name or not TRUST_NAME.search(name):
            continue
        cik, form13f = submission.get("CIK", ""), row.get("FORM13FFILENUMBER", "")
        source_id = digits(cik) or normalize_13f(form13f) or accession
        report_date = (submission.get("PERIODOFREPORT", "") or
                       row.get("REPORTCALENDARORQUARTER", ""))
        filing_date = submission.get("FILING_DATE", "")
        record = Record(
            source_kind="sec_13f", source_name="SEC Form 13F", source_record_id=source_id,
            source_locator=f"COVERPAGE:row:{line};accession:{accession}",
            source_url="https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets",
            source_snapshot_date=snapshot_date, retrieved_utc=retrieved_utc,
            as_of_date=report_date, legal_name=name,
            institution_type="institutional_manager_candidate",
            type_evidence="name contains trust; Form 13F does not establish trust charter",
            status="filing_current_in_archive", status_raw="filing_current_in_archive",
            license_scope="13f_filer_not_a_charter", public_private="not_stated",
            state=row.get("FILINGMANAGER_STATEORCOUNTRY", "") or row.get("STATEORCOUNTRY", ""),
            city=row.get("FILINGMANAGER_CITY", "") or row.get("CITY", ""),
            address1=row.get("FILINGMANAGER_STREET1", "") or row.get("STREET1", ""),
            address2=row.get("FILINGMANAGER_STREET2", "") or row.get("STREET2", ""),
            postal_code=row.get("FILINGMANAGER_ZIPCODE", "") or row.get("ZIPCODE", ""),
            cik=cik, form13f_file=form13f, crd=row.get("CRDNUMBER", ""),
            sec_file=row.get("SECFILENUMBER", ""), report_date=report_date,
            filing_date=filing_date,
            reportable_13f_value=summaries.get(accession, {}).get("TABLEVALUETOTAL", ""),
            candidate_reason="13f_manager_name_contains_trust").finish()
        ordering = (date_key(report_date), date_key(filing_date), accession)
        if source_id not in latest or ordering > latest[source_id][0]:
            latest[source_id] = (ordering, record)
    return [item[1] for item in sorted(latest.values(),
                                       key=lambda item: item[1].source_record_id)]


def load_registry(path: pathlib.Path, snapshot_date: str = "",
                  retrieved_utc: str = "") -> list[Record]:
    """Read the normalized regulator CSV described in the runbook."""
    rows = dict_rows(decode(path.read_bytes()))
    required = {"SOURCE_NAME", "SOURCE_RECORD_ID", "SOURCE_URL", "LEGAL_NAME"}
    if rows and not required.issubset(rows[0]):
        raise ResearchInputError(f"{path} is missing {sorted(required - set(rows[0]))}")
    output = []
    for line, row in enumerate(rows, start=2):
        if not all(row.get(field) for field in required):
            raise ResearchInputError(f"{path}:row:{line} lacks required identity/provenance")
        raw_status = row.get("STATUS", "")
        output.append(Record(
            source_kind="regulator_registry", source_name=row["SOURCE_NAME"],
            source_record_id=row["SOURCE_RECORD_ID"], source_locator=f"{path.name}:row:{line}",
            source_url=row["SOURCE_URL"],
            source_snapshot_date=(row.get("SOURCE_SNAPSHOT_DATE", "") or snapshot_date),
            retrieved_utc=retrieved_utc, as_of_date=row.get("AS_OF_DATE", ""),
            legal_name=row["LEGAL_NAME"],
            institution_type=row.get("INSTITUTION_TYPE", "") or "trust_company",
            type_evidence=row.get("TYPE_EVIDENCE", "") or "regulator registry row",
            status=normalize_registry_status(raw_status), status_raw=raw_status,
            regulator=row.get("REGULATOR", ""),
            charter_authority=row.get("CHARTER_AUTHORITY", ""), state=row.get("STATE", ""),
            city=row.get("CITY", ""), address1=row.get("ADDRESS1", ""),
            address2=row.get("ADDRESS2", ""), postal_code=row.get("POSTAL_CODE", ""),
            website=row.get("WEBSITE", ""), domain=row.get("DOMAIN", ""),
            license_scope=row.get("LICENSE_SCOPE", ""),
            public_private=row.get("PUBLIC_PRIVATE", ""),
            rssd=row.get("RSSD", ""), cik=row.get("CIK", ""), lei=row.get("LEI", ""),
            ein=row.get("EIN", ""), fdic_cert=row.get("FDIC_CERT", ""),
            occ_charter=row.get("OCC_CHARTER", ""),
            state_charter=row.get("STATE_CHARTER", ""),
            form13f_file=row.get("FORM13F_FILE", ""), crd=row.get("CRD", ""),
            sec_file=row.get("SEC_FILE", ""), notes=row.get("NOTES", "")).finish())
    return output
