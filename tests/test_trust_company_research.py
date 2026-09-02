from __future__ import annotations

import csv
import json
import pathlib
import sys
import tempfile
import unittest
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trust_company_matching import cluster_records, institution_rows, pair_links  # noqa: E402
from trust_company_research import (  # noqa: E402
    assert_research_output, build_report, evaluate_cases, report_build_lock,
    write_csv as write_report_csv,
)
from trust_company_schema import (  # noqa: E402
    Record, ResearchInputError, normalize_13f, sha256_file,
)
from trust_company_sources import (  # noqa: E402
    load_ffiec, load_occ, load_registry, load_sec_13f, normalize_registry_status,
)


def write_csv(path: pathlib.Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader(); writer.writerows(rows)


def write_zip(path: pathlib.Path, members: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)


class TrustCompanyResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def ffiec_file(self) -> pathlib.Path:
        path = self.base / "ffiec.zip"
        rows = [
            "ID_RSSD|NM_LGL|ENTITY_TYPE|CHTR_TYPE_CD|CHTR_AUTH_CD|STATE_ABBR_NM|CITY|URL|ID_TAX|PRIM_FED_REG",
            "101|Blue Trust, Inc.|NTC|250|2|TN|Brentwood|https://bluetrust.example|111111111|",
            "202|Ordinary Bank|NMB|200|2|GA|Atlanta|||FDIC",
        ]
        write_zip(path, {"CSV_ATTRIBUTES_ACTIVE.CSV": "\n".join(rows) + "\n"})
        return path

    def sec_file(self) -> pathlib.Path:
        path = self.base / "13f.zip"
        submission = (
            "ACCESSION_NUMBER\tFILING_DATE\tCIK\tPERIODOFREPORT\n"
            "old\t20-APR-2026\t1856022\t31-MAR-2026\n"
            "new\t21-JUL-2026\t1856022\t30-JUN-2026\n"
            "other\t21-JUL-2026\t999\t30-JUN-2026\n")
        cover = (
            "ACCESSION_NUMBER\tFILINGMANAGER_NAME\tFILINGMANAGER_STATEORCOUNTRY\t"
            "FILINGMANAGER_CITY\tFORM13FFILENUMBER\tCRDNUMBER\n"
            "old\tBlue Trust Inc\tTN\tBrentwood\t028-21235\t\n"
            "new\tBlue Trust Inc\tTN\tBrentwood\t028-21235\t\n"
            "other\tCommunity Trust Advisors\tGA\tAtlanta\t028-99999\t12345\n")
        summary = ("ACCESSION_NUMBER\tTABLEVALUETOTAL\nold\t10\nnew\t20\nother\t30\n")
        write_zip(path, {"SUBMISSION.tsv": submission, "COVERPAGE.tsv": cover,
                         "SUMMARYPAGE.tsv": summary})
        return path

    def registry_file(self) -> pathlib.Path:
        path = self.base / "tn.csv"
        fields = ["source_name", "source_record_id", "source_url", "as_of_date",
                  "legal_name", "institution_type", "status", "regulator", "state",
                  "city", "website", "rssd", "cik", "state_charter"]
        write_csv(path, fields, [{
            "source_name": "Tennessee DFI", "source_record_id": "tn-blue",
            "source_url": "https://tn.example/trust", "as_of_date": "2026-09-01",
            "legal_name": "Blue Trust, Inc.", "institution_type": "public_trust_company",
            "status": "active", "regulator": "TDFI", "state": "TN",
            "city": "Brentwood", "website": "https://bluetrust.example",
            "rssd": "101", "cik": "1856022", "state_charter": "blue-charter",
        }])
        return path

    def occ_file(self) -> pathlib.Path:
        import pandas as pd
        path = self.base / "occ.xlsx"
        table = pd.DataFrame([{
            "CHARTER NO": "25173", "NAME": "Example National Trust Company",
            "ADDRESS (LOC)": "1 Main St", "CITY": "Wilmington", "STATE": "DE",
            "CERT": "59194", "RSSD": "5397639",
        }])
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            pd.DataFrame([["Trust Banks Active As of 7/31/2026"]]).to_excel(
                writer, sheet_name="Trust", header=False, index=False)
            table.to_excel(writer, sheet_name="Trust", startrow=3, index=False)
        return path

    def known_cases_file(self) -> pathlib.Path:
        path = self.base / "cases.csv"
        fields = ["case_id", "expected_name", "expected_state", "identifier_type",
                  "identifier_value", "required_source_kind", "expected_regulatory_status"]
        write_csv(path, fields, [{
            "case_id": "blue", "expected_name": "Blue Trust Inc", "expected_state": "TN",
            "identifier_type": "cik", "identifier_value": "1856022",
            "required_source_kind": "regulator_registry",
            "expected_regulatory_status": "verified_trust_company",
        }])
        return path

    def test_ffiec_adapter_excludes_nontrust_bank(self):
        rows = load_ffiec(self.ffiec_file())
        self.assertEqual([row.rssd for row in rows], ["101"])
        self.assertEqual(rows[0].institution_type, "non_depository_trust_company")

    def test_sec_adapter_keeps_latest_filing_and_does_not_infer_charter(self):
        rows = load_sec_13f(self.sec_file())
        blue = next(row for row in rows if row.cik == "0001856022")
        self.assertEqual(blue.report_date, "30-JUN-2026")
        self.assertEqual(blue.reportable_13f_value, "20")
        self.assertEqual(blue.institution_type, "institutional_manager_candidate")
        self.assertEqual(blue.crd, "")

    def test_occ_adapter_reads_trust_sheet_and_embedded_snapshot_date(self):
        rows = load_occ(self.occ_file())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].occ_charter, "25173")
        self.assertEqual(rows[0].rssd, "5397639")
        self.assertEqual(rows[0].source_snapshot_date, "2026-07-31")
        self.assertEqual(rows[0].institution_type, "national_trust_bank")

    def test_form13f_normalization_does_not_truncate_long_file_numbers(self):
        self.assertEqual(normalize_13f("028-123456"), "028-123456")

    def test_registry_requires_source_url(self):
        path = self.base / "bad.csv"
        write_csv(path, ["source_name", "source_record_id", "source_url", "legal_name"],
                  [{"source_name": "state", "source_record_id": "1", "source_url": "",
                    "legal_name": "Example Trust"}])
        with self.assertRaisesRegex(ResearchInputError, "provenance"):
            load_registry(path)

    def test_known_case_with_identifier_never_falls_back_to_name_and_state(self):
        record = Record(
            source_kind="sec_13f", source_name="SEC Form 13F", source_record_id="1",
            legal_name="Blue Trust Inc", state="TN", cik="111",
            status="filing_current_in_archive").finish()
        institutions = institution_rows([[record]])
        cases = [{"CASE_ID": "blue", "EXPECTED_NAME": "Blue Trust Inc",
                  "EXPECTED_STATE": "TN", "IDENTIFIER_TYPE": "cik",
                  "IDENTIFIER_VALUE": "222"}]
        result = evaluate_cases(cases, [record], institutions)[0]
        self.assertEqual(result["matches"], 0)
        self.assertFalse(result["pass"])

    def test_inactive_registry_record_is_visible_but_not_current_or_prospect(self):
        path = self.base / "inactive.csv"
        fields = ["source_name", "source_record_id", "source_url", "legal_name",
                  "institution_type", "type_evidence", "status", "state"]
        write_csv(path, fields, [{
            "source_name": "State DFI", "source_record_id": "old-1",
            "source_url": "https://state.example/old-1", "legal_name": "Old Trust Co",
            "institution_type": "public_trust_company",
            "type_evidence": "historical regulator record", "status": "Surrendered",
            "state": "TN",
        }])
        output = self.base / "inactive-report"
        summary = build_report(None, None, [path], None, output)
        self.assertEqual(summary["counts"]["historicalOrInactive"], 1)
        self.assertEqual(summary["counts"]["regulatedUniverse"], 0)
        self.assertEqual(summary["counts"]["prospectCandidates"], 0)
        with (output / "institutions.csv").open(encoding="utf-8-sig") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["regulatory_status"], "historical_or_inactive")
        self.assertEqual(row["identity_status"], "historical_or_inactive")
        self.assertEqual((output / "regulated_universe.csv").read_text(
            encoding="utf-8-sig").count("\n"), 1)

    def test_registry_status_normalization_fails_closed(self):
        for value in ("Inactive", "Not active", "No longer active", "Not open",
                      "No longer open", "Not in good standing",
                      "No longer in good standing", "License not current", "Not licensed", "Charter surrendered",
                      "Application denied", "Historical record", "Revoked", "Expired"):
            with self.subTest(value=value):
                self.assertEqual(normalize_registry_status(value), "inactive")
        for value in ("Active", "Licensed", "Open", "Good Standing"):
            with self.subTest(value=value):
                self.assertEqual(normalize_registry_status(value), "active")
        self.assertEqual(normalize_registry_status("Approved"), "unknown")
        self.assertEqual(normalize_registry_status(""), "unknown")

    def test_report_build_lock_rejects_a_second_writer(self):
        output = self.base / "locked-report"
        with report_build_lock(output):
            with self.assertRaisesRegex(ResearchInputError, "already publishing"):
                with report_build_lock(output):
                    self.fail("overlapping writer acquired the report lock")

    def test_generated_csv_escapes_formula_cells_without_mutating_values(self):
        path = self.base / "safe.csv"
        rows = [{"name": "=HYPERLINK(\"bad\")", "note": "  -1+2", "safe": "Blue Trust"}]
        write_report_csv(path, rows, ["name", "note", "safe"])
        with path.open(encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["name"], "'=HYPERLINK(\"bad\")")
        self.assertEqual(row["note"], "'  -1+2")
        self.assertEqual(row["safe"], "Blue Trust")
        self.assertEqual(rows[0]["name"], "=HYPERLINK(\"bad\")")

    def test_only_shared_strong_identifier_auto_links(self):
        a = Record(source_kind="ffiec_nic", source_name="a", source_record_id="1",
                   legal_name="Example Trust", state="TN", domain="example.com", rssd="99").finish()
        b = Record(source_kind="sec_13f", source_name="b", source_record_id="2",
                   legal_name="Example Trust Inc", state="TN", domain="example.com").finish()
        links = pair_links([a, b])
        self.assertEqual(links[0]["action"], "review")
        self.assertEqual(len(cluster_records([a, b], links)), 2)
        b.rssd = "99"
        links = pair_links([a, b])
        self.assertEqual(links[0]["action"], "auto_link")

    def test_crd_is_observed_but_never_an_identity_key(self):
        a = Record(source_kind="ffiec_nic", source_name="a", source_record_id="1",
                   legal_name="Father Trust", state="TN", crd="104605").finish()
        b = Record(source_kind="sec_13f", source_name="b", source_record_id="2",
                   legal_name="Unrelated Manager", state="GA", crd="104605").finish()
        self.assertEqual(pair_links([a, b]), [])

    def test_conflicting_strong_identifiers_fail_closed(self):
        a = Record(source_kind="ffiec_nic", source_name="a", source_record_id="1",
                   legal_name="Example Trust", rssd="1", cik="10").finish()
        b = Record(source_kind="regulator_registry", source_name="b", source_record_id="2",
                   legal_name="Example Trust", rssd="2", cik="10").finish()
        links = pair_links([a, b])
        self.assertEqual(links[0]["action"], "block_conflict")
        self.assertEqual(len(cluster_records([a, b], links)), 2)

    def test_end_to_end_report_is_research_only_and_blue_case_passes(self):
        output = self.base / "report"
        summary = build_report(self.ffiec_file(), self.sec_file(), [self.registry_file()],
                               self.known_cases_file(), output)
        self.assertFalse(summary["productionEligible"])
        self.assertEqual(summary["counts"]["knownCaseFailures"], 0)
        self.assertEqual(summary["counts"]["verifiedTrustCompanies"], 1)
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["productionEligible"])
        for name, expected in manifest["outputs"].items():
            self.assertEqual(sha256_file(output / name), expected)
        self.assertTrue((output / "regulated_universe.csv").exists())
        self.assertTrue((output / "prospect_candidates.csv").exists())
        with (output / "institutions.csv").open(encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        blue = next(row for row in rows if row["cik"] == "0001856022")
        self.assertEqual(blue["source_count"], "3")
        self.assertEqual(blue["crd_observed_not_identity_key"], "")

    def test_repository_output_boundary_rejects_production_directories(self):
        with self.assertRaisesRegex(ResearchInputError, "data/research"):
            assert_research_output(ROOT / "webapp" / "data" / "trusts")

    def test_production_build_entrypoints_do_not_import_research_pipeline(self):
        for path in (ROOT / "run.py", ROOT / "src/rebuild_webapp.py", ROOT / "src/build_api.sh"):
            self.assertNotIn("trust_company_research", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
