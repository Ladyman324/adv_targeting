"""Working-location precedence must not weaken contact identity or territory."""
import pathlib
import sys
import tempfile
import unittest

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from contact_work_locations import (clean_street, placement_key,
                                    select_candidates, source_record)
import export_geojson


ZIPS = {"11366": ["NY", 40.73, -73.79, 1],
        "63131": ["MO", 38.61, -90.45, 1]}


def roster_row(**changes):
    row = {
        "name": "Aaron Fayzulayev",
        "email": "aaron@example.com", "firm_crd": "250",
        "city": "Fresh Meadows", "state": "NY",
        "location_street": "181-22 Union Turnpike, Fresh Meadows, NY 11366",
        "location_zip": "11366", "location_lat": "40.727246",
        "location_lon": "-73.786826", "source_file": "edward_jones_new.csv",
    }
    row.update(changes)
    return row


def test_roster_published_office_replaces_hq_only_with_email_and_firm_agreement():
    contacts = {"7320366": {"e": "aaron@example.com", "t": "high", "src": "Edward Jones"}}
    rows, audit = select_candidates(contacts, pd.DataFrame([roster_row()]), [],
                                    {("7320366", "250")}, ZIPS)
    assert len(rows) == 1
    assert rows.iloc[0]["branch_state"] == "NY"
    assert rows.iloc[0]["branch_street1"] == "181-22 Union Turnpike"
    assert rows.iloc[0]["location_source"] == "firm_roster"
    assert not audit
    no_firm, _ = select_candidates(contacts, pd.DataFrame([roster_row()]), [],
                                    {("7320366", "999")}, ZIPS)
    assert no_firm.empty


def test_shared_email_and_ambiguous_roster_offices_never_set_territory():
    shared = {"1": {"e": "aaron@example.com", "t": "high"},
              "2": {"e": "aaron@example.com", "t": "high"}}
    rows, _ = select_candidates(shared, pd.DataFrame([roster_row()]), [],
                                {("1", "250"), ("2", "250")}, ZIPS)
    assert rows.empty
    review_duplicate = {"1": {"e": "aaron@example.com", "t": "high"},
                        "2": {"e": "aaron@example.com", "t": "review"}}
    rows, _ = select_candidates(review_duplicate,
                                pd.DataFrame([roster_row()]), [],
                                {("1", "250")}, ZIPS)
    assert rows.empty
    single = {"1": {"e": "aaron@example.com", "t": "high"}}
    rows, audit = select_candidates(
        single, pd.DataFrame([roster_row(),
                              roster_row(location_street="200 Other Street")]),
        [], {("1", "250")}, ZIPS)
    assert rows.empty
    assert audit[0]["result"] == "ambiguous_roster_offices"
    names = pd.DataFrame([roster_row(),
                          roster_row(name="Another Advisor")])
    rows, _ = select_candidates(single, names, [], {("1", "250")}, ZIPS)
    assert rows.empty


def test_act_fallback_requires_approved_id_and_exact_email():
    contacts = {"1": {"e": "aaron@example.com", "t": "confirmed",
                      "src": "CRM", "aid": "act-1", "fc": "250"}}
    act = [{"id": "act-1", "emailAddress": "aaron@example.com",
            "businessAddress": {"line1": "181-22 Union Turnpike",
                                "city": "Fresh Meadows", "state": "NY",
                                "postalCode": "11366", "latitude": 40.727246,
                                "longitude": -73.786826}}]
    rows, _ = select_candidates(contacts, pd.DataFrame(columns=["email", "firm_crd", "name"]),
                                act, {("1", "250")}, ZIPS)
    assert len(rows) == 1
    assert rows.iloc[0]["location_source"] == "ACT"
    act[0]["emailAddress"] = "someone-else@example.com"
    rows, _ = select_candidates(contacts, pd.DataFrame(columns=["email", "firm_crd", "name"]),
                                act, {("1", "250")}, ZIPS)
    assert rows.empty


def test_coordinate_contradicting_postal_area_is_not_used():
    row = source_record(roster_row(location_lat="38.61", location_lon="-90.45"),
                        "firm_roster", ZIPS)
    assert row is not None
    assert pd.isna(row["lat"])
    assert clean_street("181-22 Union Turnpike, Fresh Meadows, NY 11366",
                        "Fresh Meadows", "NY", "11366") == "181-22 Union Turnpike"
    assert placement_key("181-22 Union Turnpike", "Fresh Meadows", "11366") == (
        "181-22 UNION TURNPIKE|FRESH MEADOWS|11366")


def test_export_prefers_roster_row_when_sec_branch_has_same_address_key():
    with tempfile.TemporaryDirectory() as directory:
        old = export_geojson.INTERIM
        export_geojson.INTERIM = pathlib.Path(directory)
        try:
            pd.DataFrame([{"advisor_crd": "1", "firm_crd": "250",
                           "addr_key": "100 MAIN ST|NEW YORK|10001",
                           "uncertain": False, "home_label": "",
                           "location_type": "office"}]).to_parquet(
                pathlib.Path(directory) / "advisor_placement.parquet")
            rows = pd.DataFrame([
                {"advisor_crd": "1", "firm_crd": "250",
                 "branch_street1": "100 MAIN ST", "branch_street2": "Floor 5",
                 "branch_city": "NEW YORK", "branch_postal": "10001",
                 "location_source": ""},
                {"advisor_crd": "1", "firm_crd": "250",
                 "branch_street1": "100 MAIN ST", "branch_street2": "",
                 "branch_city": "NEW YORK", "branch_postal": "10001",
                 "location_source": "firm_roster"},
            ])
            result = export_geojson.apply_placement(rows)
            assert len(result) == 1
            assert result.iloc[0]["location_source"] == "firm_roster"
            assert result.iloc[0]["branch_street2"] == ""
        finally:
            export_geojson.INTERIM = old


class ContactWorkLocationTests(unittest.TestCase):
    def test_roster_precedence(self):
        test_roster_published_office_replaces_hq_only_with_email_and_firm_agreement()

    def test_ambiguous_email_and_offices(self):
        test_shared_email_and_ambiguous_roster_offices_never_set_territory()

    def test_act_identity(self):
        test_act_fallback_requires_approved_id_and_exact_email()

    def test_coordinate_guard(self):
        test_coordinate_contradicting_postal_area_is_not_used()

    def test_equal_address_export(self):
        test_export_prefers_roster_row_when_sec_branch_has_same_address_key()
