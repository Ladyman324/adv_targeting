"""Approved identity address evidence and ACT primary location precedence."""
import copy
import hashlib
import pathlib
import sys
import unittest

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from act_address_review import (
    build_address_reviews, build_contact_create_profiles, build_crm_directory, usable,
)
from contact_work_locations import select_candidates


class ActAddressReviewTests(unittest.TestCase):
    def setUp(self):
        self.mapping = {"2066775": "act-chris"}
        self.contacts = {"2066775": {"aid": "act-chris", "t": "confirmed", "src": "CRM",
                         "e": "c.tolman@example.com", "fc": "8174", "can_email": True}}
        self.act = [{"id": "act-chris", "firstName": "Christopher", "lastName": "Tolman",
            "emailAddress": "c.tolman@example.com",
            "businessAddress": {"line1": "1285 Avenue of the Americas", "line2": "Floor 15",
                "city": "New York", "state": "NY", "postalCode": "10019", "latitude": 40.76, "longitude": -73.98},
            "homeAddress": {"country": "John/Chris/Regina/Alec"}}]
        self.roster = [{"name": "Christopher Tolman", "email": "c.tolman@example.com", "firm_crd": "8174",
            "company": "UBS", "city": "Austin", "state": "TX", "location_street": "401 West 4th Street",
            "location_street2": "Suite 3100", "location_zip": "78701", "source_file": "ubs_2026-10-25.csv",
            "profile_url": "https://example.com/chris", "location_lat": "30.26", "location_lon": "-97.74"}]
        self.employment = {("2066775", "8174")}
        self.zips = {"10019": ["NY", 40.76, -73.98, 1], "78701": ["TX", 30.26, -97.74, 1]}

    def build(self):
        return build_address_reviews(self.mapping, self.contacts, self.act, self.roster, self.employment)

    def test_evidence_uses_approved_contact_and_preserves_full_roster_address(self):
        before = copy.deepcopy((self.contacts, self.act, self.roster))
        row = self.build()["2066775"]
        self.assertEqual(row["contactId"], "act-chris")
        self.assertEqual(row["identity"]["lastName"], "Tolman")
        self.assertEqual(row["candidates"][0]["address"]["line2"], "Suite 3100")
        self.assertEqual((self.contacts, self.act, self.roster), before)

    def test_activity_only_link_does_not_create_address_authority(self):
        self.mapping = {}
        self.assertEqual(self.build(), {})

    def test_wrong_or_ambiguous_act_link_is_rejected(self):
        self.contacts["2066775"]["aid"] = "someone-else"
        self.assertEqual(self.build(), {})
        self.contacts["2066775"]["aid"] = "act-chris"
        self.mapping["1234567"] = "ACT-CHRIS"
        self.assertEqual(self.build(), {})

    def test_shared_email_different_person_and_firm_never_offer_address(self):
        self.contacts["9999999"] = {"e": "c.tolman@example.com", "t": "review"}
        self.assertEqual(self.build()["2066775"]["candidates"], [])
        del self.contacts["9999999"]
        self.roster.append(dict(self.roster[0], name="Someone Else"))
        self.assertEqual(self.build()["2066775"]["candidates"], [])
        self.roster.pop()
        self.employment = set()
        self.assertEqual(self.build()["2066775"]["candidates"], [])

    def test_approved_act_primary_wins_over_conflicting_roster(self):
        before = copy.deepcopy(self.contacts)
        rows, _ = select_candidates(self.contacts, pd.DataFrame(self.roster), self.act,
                                    self.employment, self.zips, self.mapping)
        self.assertEqual(rows.iloc[0]["location_source"], "ACT")
        self.assertEqual(rows.iloc[0]["branch_state"], "NY")
        self.assertEqual(rows.iloc[0]["branch_street2"], "Floor 15")
        self.assertEqual(self.contacts, before)

    def test_approved_identity_does_not_depend_on_selected_email_route(self):
        self.contacts["2066775"]["e"] = "other-approved-email@example.com"
        rows, _ = select_candidates(self.contacts, pd.DataFrame(self.roster), self.act,
                                    self.employment, self.zips, self.mapping)
        self.assertEqual(rows.iloc[0]["location_source"], "ACT")

    def test_secondary_never_replaces_primary_map_location(self):
        self.act[0]["homeAddress"].update(line1="401 West 4th Street", city="Austin", state="TX", postalCode="78701")
        rows, _ = select_candidates(self.contacts, pd.DataFrame(self.roster), self.act,
                                    self.employment, self.zips, self.mapping)
        self.assertEqual(rows.iloc[0]["branch_state"], "NY")

    def test_unusable_primary_uses_roster_and_preserves_suite(self):
        self.act[0]["businessAddress"] = {"city": "New York", "state": "NY"}
        rows, _ = select_candidates(self.contacts, pd.DataFrame(self.roster), self.act,
                                    self.employment, self.zips, self.mapping)
        self.assertEqual(rows.iloc[0]["location_source"], "firm_roster")
        self.assertEqual(rows.iloc[0]["branch_street2"], "Suite 3100")

    def test_stale_ledger_id_cannot_supply_act_map_location(self):
        rows, _ = select_candidates(self.contacts, pd.DataFrame(self.roster), self.act,
                                    self.employment, self.zips, {"2066775": "different-act-id"})
        self.assertEqual(rows.iloc[0]["location_source"], "firm_roster")

    def test_directory_keeps_unlinked_contacts_distinct_and_protects_private_rows(self):
        shared = dict(self.act[0], id="unlinked", fullName="Shared Contact",
                      emailAddress="share@example.com", customFields={"crd": "2222222"})
        private = dict(shared, id="private", fullName="Private Contact",
                       emailAddress="private@example.com", isPrivate=True)
        remote = dict(shared, id="remote", fullName="Remote Contact",
                      businessAddress=dict(shared["businessAddress"], latitude=30.26, longitude=-97.74))
        output = build_crm_directory(self.mapping, [*self.act, shared, private, remote], self.zips)
        self.assertEqual([row["name"] for row in output["contacts"]], ["Remote Contact", "Shared Contact"])
        self.assertIsNone(output["contacts"][0]["coordinates"])
        self.assertEqual(output["contacts"][1]["coordinates"], [40.76, -73.98])
        self.assertNotIn("id", output["contacts"][1])
        self.assertEqual(output["claimedCrds"], ["2222222"])
        self.assertIn(hashlib.sha256(b"private@example.com").hexdigest(), output["emailHashes"])

    def test_creation_fields_and_address_require_the_same_firm_and_email(self):
        row = self.contacts["2066775"]
        row.pop("aid")
        row.update(t="high", tn="Advocate Partners", ti="Advisor", w="+12125550000",
                   pu="https://example.com/chris")
        def profiles():
            return build_contact_create_profiles({}, self.contacts, self.roster, self.employment)
        item = profiles()["2066775"]
        self.assertEqual(item["department"], "Advocate Partners")
        self.assertEqual(item["businessAddress"]["line2"], "Suite 3100")
        self.assertEqual(item["emailHash"], hashlib.sha256(row["e"].encode()).hexdigest())
        self.roster.append(dict(self.roster[0], location_street="Other Office"))
        self.assertNotIn("businessAddress", profiles()["2066775"])
        self.contacts["1234567"] = {"e": row["e"], "t": "review"}
        self.assertEqual(profiles(), {})

    def test_partners_is_not_a_secondary_address(self):
        self.assertFalse(usable(self.act[0]["homeAddress"]))


if __name__ == "__main__":
    unittest.main()
