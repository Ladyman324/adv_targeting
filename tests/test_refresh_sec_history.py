"""SEC source failure injection: complete archives can still contain bad data."""
import csv
import gzip
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import refresh_core as core
import refresh_history as history
import refresh_sec_history as sec
import refresh_acquire as acquire
import refresh_review as review
from test_refresh_acquire import pair


def individual_nodes(count=100, offset=0):
    result = []
    for number in range(count):
        record = ET.Element("Indvl")
        ET.SubElement(record, "Info", indvlPK=str(number + offset + 1), firstNm="Person", lastNm=str(number + offset))
        ET.SubElement(ET.SubElement(record, "OthrNms"), "OthrNm", firstNm="Nickname", lastNm=str(number + offset))
        employer = ET.SubElement(ET.SubElement(record, "CrntEmps"), "CrntEmp", orgPK="100", orgNm="Fixture firm",
                                 str1="Fixture street", city="Atlanta", state="GA", postlCd="30309")
        ET.SubElement(ET.SubElement(employer, "CrntRgstns"), "CrntRgstn", regCat="AG", st="APPROVED")
        result.append(record)
    return result


def firm_nodes(count=100):
    result = []
    for number in range(count):
        record = ET.Element("Firm")
        ET.SubElement(record, "Info", FirmCrdNb=str(number + 1), BusNm="Fixture " + str(number), LegalNm="Legal " + str(number))
        ET.SubElement(record, "MainAddr", City="Atlanta", State="GA", Strt1="Fixture street", PostlCd="30309", Cntry="United States")
        ET.SubElement(ET.SubElement(record, "NoticeFiled"), "States", RgltrCd="GA", St="APPROVED")
        result.append(record)
    return result


def xml(records):
    root = ET.Element("Root"); root.extend(records)
    return ET.tostring(root, encoding="utf-8")


def write_zip(path, records, parts=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for number in range(parts): archive.writestr("part-" + str(number) + ".xml", xml(records[number::parts]))
    return path


def write_gzip(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as stream: stream.write(xml(records))
    return path


def write_adv(path, count=100, amount="1000"):
    from adv import COLS, FLAG_FIELDS
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted(set(COLS.values()))
    with path.open("w", encoding="latin-1", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        for number in range(count):
            row = {key: "1" for key in fields}
            row.update({COLS[key]: "Y" for key in FLAG_FIELDS})
            row.update({COLS["crd"]: str(number + 1), COLS["name"]: "Firm " + str(number),
                        COLS["legal_name"]: "Legal " + str(number), COLS["city"]: "Atlanta", COLS["state"]: "GA",
                        COLS["raum_total"]: amount})
            writer.writerow(row)
    return path


class SecHistoricalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.stage = Path(self.temp.name) / "stage"; self.stage.mkdir()
        self.old = write_zip(self.stage / "data/raw/IA_INDVL_Feed_09_22_2026.xml.zip", individual_nodes())
        self.candidate = write_zip(Path(self.temp.name) / "IA_INDVL_Feed_10_23_2026.xml.zip", individual_nodes())

    def controller(self):
        root = Path(self.temp.name) / "repo"; root.mkdir(exist_ok=True)
        pair(self.stage / "data/raw")
        core.atomic_json(self.stage / ".refresh-workspace.json", {"runId": "fixture", "ownerToken": "token"})
        directory = core.run_path(root, "fixture"); directory.mkdir(parents=True, exist_ok=True)
        value = {"schemaVersion": 1, "id": "fixture", "ownerToken": "token", "workspaceRoot": str(self.stage),
                 "baselineRoot": str(root), "mode": "sec-only", "firms": [], "codeFiles": {}, "codeNormalization": {},
                 "inputs": core.facts(core.source_paths(self.stage, "sec-only"), self.stage), "imports": [], "attempts": [],
                 "steps": {}, "warnings": [], "status": "ready"}
        core.save_run(directory, value)
        return root

    def assess(self, records=None, parts=2):
        if records is not None: write_zip(self.candidate, records, parts)
        return history.compare_source(self.stage, "sec_individuals", self.candidate)

    def codes(self, records=None, parts=2):
        result = self.assess(records, parts)
        self.assertEqual(result["status"], "held")
        return {item["code"] for item in result["issues"]}

    def test_identical_records_and_order_changes_are_accepted(self):
        for records in (individual_nodes(), list(reversed(individual_nodes()))):
            result = self.assess(records)
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(result["continuity"]["retained"], 100)
            self.assertEqual(result["candidate"]["fields"]["otherNameForms"], 100)

    def test_valid_closed_xml_partial_download_is_held(self):
        self.assertIn("SEC_CRDS_DISAPPEARED", self.codes(individual_nodes(50)))

    def test_same_count_wrong_crds_are_held(self):
        self.assertIn("SEC_CRDS_DISAPPEARED", self.codes(individual_nodes(offset=1000)))

    def test_archive_part_count_change_is_explicit_even_when_crds_match(self):
        self.assertIn("SEC_ARCHIVE_PART_COUNT_CHANGED", self.codes(individual_nodes(), parts=1))

    def test_names_aliases_employment_and_addresses_are_independent_checks(self):
        for changed in ("names", "aliases", "employment", "addresses", "state"):
            records = individual_nodes()
            for record in records[:30]:
                if changed == "names": record.find("Info").attrib.update(firstNm="", lastNm="")
                if changed == "aliases": record.remove(record.find("OthrNms"))
                if changed == "employment": record.remove(record.find("CrntEmps"))
                if changed in {"addresses", "state"}:
                    employer = record.find("./CrntEmps/CrntEmp")
                    for key in (("str1", "city", "state", "postlCd") if changed == "addresses" else ("state",)):
                        employer.set(key, "")
            self.assertIn("SEC_FIELD_COVERAGE_LOST", self.codes(records))

    def test_same_count_content_scramble_is_not_hidden_by_presence(self):
        records = individual_nodes()
        for record in records: record.find("Info").set("lastNm", "Changed")
        self.assertIn("SEC_RECORD_VALUES_CHANGED", self.codes(records))

    def test_duplicate_or_missing_native_crd_is_a_format_error(self):
        for key in ("", "1"):
            records = individual_nodes(); records[1].find("Info").set("indvlPK", key)
            self.assertIn("SEC_FORMAT_INVALID", self.codes(records))

    def test_truncated_zip_and_gzip_are_rejected(self):
        self.candidate.write_bytes(self.candidate.read_bytes()[:25])
        self.assertIn("SEC_FORMAT_INVALID", self.codes())
        firms = write_gzip(Path(self.temp.name) / "bad.xml.gz", firm_nodes())
        firms.write_bytes(firms.read_bytes()[:-8])
        self.assertEqual(sec.compare(firms, None, "sec_firms")["status"], "held")

    def test_xml_namespace_change_is_held_until_downstream_parser_supports_it(self):
        with zipfile.ZipFile(self.candidate, "w") as archive:
            archive.writestr("part.xml", '<Root xmlns="urn:changed"><Indvl><Info indvlPK="1" firstNm="Fixture" lastNm="Person"/></Indvl></Root>')
        self.assertIn("SEC_FORMAT_INVALID", self.codes())

    def test_dtd_or_entity_expansion_is_refused(self):
        with zipfile.ZipFile(self.candidate, "w") as archive:
            archive.writestr("part.xml", '<!DOCTYPE Root [<!ENTITY x "value">]><Root><Indvl><Info indvlPK="1" firstNm="&x;"/></Indvl></Root>')
        self.assertIn("SEC_FORMAT_INVALID", self.codes())

    def test_utf16_cannot_bypass_dtd_guard(self):
        content = '<!DOCTYPE Root [<!ENTITY x "value">]><Root><Indvl><Info indvlPK="1" firstNm="&x;"/></Indvl></Root>'
        with zipfile.ZipFile(self.candidate, "w") as archive:
            archive.writestr("part.xml", content.encode("utf-16"))
        result = self.assess()
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["issues"][0]["reasonCode"], "XML_ENCODING_UNSUPPORTED")

    def test_adv_archive_size_is_checked_before_crc_decompression(self):
        file = Path(self.temp.name) / "oversized.zip"
        with zipfile.ZipFile(file, "w") as archive: archive.writestr("firms.csv", b"x" * 100)
        with patch.object(sec, "MAX_MEMBER_BYTES", 50), patch.object(zipfile.ZipFile, "testzip") as crc:
            result = sec.compare(file, None, "sec_adv")
            self.assertEqual(result["status"], "held")
            crc.assert_not_called()

    def test_wrong_feed_kind_cannot_be_accepted(self):
        write_zip(self.candidate, firm_nodes())
        self.assertIn("SEC_FORMAT_INVALID", self.codes())

    def test_firm_capitalized_main_address_schema_is_counted(self):
        file = write_gzip(Path(self.temp.name) / "firms.xml.gz", firm_nodes())
        result = sec.compare(file, file, "sec_firms")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["candidate"]["fields"]["locationRecords"], 100)
        self.assertEqual(result["candidate"]["fields"]["streetRecords"], 100)

    def test_adv_uses_native_crds_and_detects_asset_unit_changes(self):
        old = write_adv(Path(self.temp.name) / "old.csv")
        new = write_adv(Path(self.temp.name) / "new.csv")
        self.assertEqual(sec.compare(new, old, "sec_adv")["status"], "accepted")
        write_adv(new, amount="1000000")
        result = sec.compare(new, old, "sec_adv")
        self.assertIn("SEC_AGGREGATE_ASSETS_CHANGED", {item["code"] for item in result["issues"]})

    def test_adv_number_contract_agrees_with_actual_consumer(self):
        from adv import _num
        import pandas as pd
        values = [" 1,000.00 ", ".00", "1e3", "$1,000", "1 000", "1_000", "not numeric"]
        consumed = _num(pd.Series(values))
        for index, value in enumerate(values):
            self.assertEqual(sec._number(value) is not None, pd.notna(consumed.iloc[index]), msg=value)
        file = write_adv(Path(self.temp.name) / "currency.csv", amount="$1,000")
        self.assertEqual(sec.compare(file, None, "sec_adv")["status"], "held")

    def test_adv_missing_headers_truncated_rows_and_negative_assets_are_held(self):
        file = write_adv(Path(self.temp.name) / "adv.csv")
        with file.open("a") as stream: stream.write("partial,row\n")
        self.assertEqual(sec.compare(file, None, "sec_adv")["status"], "held")
        write_adv(file, amount="-1000")
        self.assertEqual(sec.compare(file, None, "sec_adv")["status"], "held")
        file.write_text("Organization CRD#,Primary Business Name\n1,Fixture\n", encoding="latin-1")
        self.assertEqual(sec.compare(file, None, "sec_adv")["status"], "held")

    def test_adv_business_fields_cannot_disappear_behind_stable_assets_and_counts(self):
        from adv import COLS
        old = write_adv(Path(self.temp.name) / "old.csv")
        new = write_adv(Path(self.temp.name) / "new.csv")
        with new.open(encoding="latin-1", newline="") as stream:
            reader = csv.DictReader(stream); fields, records = reader.fieldnames, list(reader)
        for row in records:
            row[COLS["acct_total"]] = ""
            row[COLS["g_select_advisers"]] = "N"
        with new.open("w", encoding="latin-1", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(records)
        result = sec.compare(new, old, "sec_adv")
        self.assertEqual(result["status"], "held")
        lost = {item.get("field") for item in result["issues"] if item["code"] == "SEC_FIELD_COVERAGE_LOST"}
        self.assertIn("adv:acct_total", lost)
        self.assertIn("advYes:g_select_advisers", lost)
        self.assertEqual(result["candidate"]["reportedRaumTotal"], result["baseline"]["reportedRaumTotal"])

    def test_adv_source_selection_never_includes_individual_xml_zip(self):
        file = self.stage / "data/raw/ia050126.zip"
        with zipfile.ZipFile(file, "w") as archive: archive.writestr("firms.csv", "fixture")
        self.assertEqual(history.baseline_paths(self.stage, "sec_adv"), [file])
        self.assertEqual(acquire._source_files(self.stage, "sec_adv"), [file])

    def test_sec_baseline_selection_uses_date_not_lexical_month_order(self):
        file = write_zip(self.stage / "data/raw/IA_INDVL_Feed_01_01_2027.xml.zip", individual_nodes())
        self.assertEqual(history.baseline_paths(self.stage, "sec_individuals"), [file])

    def test_import_hold_and_explicit_review_preserve_selected_good_feed(self):
        root = self.controller()
        original = self.old.read_bytes()
        write_zip(self.candidate, individual_nodes(90))
        with self.assertRaisesRegex(core.RefreshError, "SEC_CRDS_DISAPPEARED"):
            acquire.import_snapshot(root, "fixture", "sec_individuals", self.candidate)
        self.assertEqual(self.old.read_bytes(), original)
        self.assertFalse((self.old.parent / self.candidate.name).exists())
        file = next((self.stage / "data/refresh/acquisition_reviews/sec_individuals").glob("*/comparison.json"))
        report = core.read_json(file)
        failed = core.read_json(core.run_path(root, "fixture") / "run.json")
        self.assertEqual(failed["errorCode"], "SOURCE_HISTORY_HELD")
        self.assertEqual(failed["attempts"][-1]["historyComparisonId"], file.parent.name)
        self.assertIn("SEC_CRDS_DISAPPEARED", failed["attempts"][-1]["historyIssueCodes"])
        review.review(root, "fixture", file.parent.name, "approve", "Fixture operator",
                      "Confirmed genuine SEC registration population change", sorted({item["code"] for item in report["issues"]}),
                      source_id="sec_individuals")
        result = acquire.import_snapshot(root, "fixture", "sec_individuals", self.candidate)
        self.assertEqual(result["imports"][-1]["records"], 90)
        self.assertEqual(result["imports"][-1]["historicalAcceptance"]["status"], "accepted")
        self.assertFalse((root / "data/raw").exists())

    def test_adv_import_does_not_displace_or_alter_the_individual_feed(self):
        prior_csv = write_adv(Path(self.temp.name) / "prior.csv")
        prior_zip = self.stage / "data/raw/ia050126.zip"
        with zipfile.ZipFile(prior_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("firms.csv", prior_csv.read_bytes())
        incoming = write_adv(Path(self.temp.name) / "current.csv")
        root = self.controller()
        before = self.old.read_bytes()
        result = acquire.import_snapshot(root, "fixture", "sec_adv", incoming)
        self.assertEqual(result["imports"][-1]["records"], 100)
        self.assertTrue(self.old.exists())
        self.assertEqual(self.old.read_bytes(), before)
        self.assertEqual(acquire._source_files(self.stage, "sec_individuals"), [self.old])
        self.assertFalse((root / "data/raw").exists())


if __name__ == "__main__":
    unittest.main()
