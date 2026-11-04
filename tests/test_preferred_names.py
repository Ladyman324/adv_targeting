from __future__ import annotations

import copy
import json
import pathlib
import sys
import tempfile
import unittest

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from preferred_names import (ACT_PRIMARY_SOURCE, ROSTER_SOURCE, SEC_SOURCE,
                             build_act_primary_email_index, choose_contact_greeting,
                             clean_act_salutation, load_act_primary_email_index,
                             preferred_display_name, roster_names_for_contact)
from roster_greetings import valid_greeting
from identity_schema import content_hash
from contact_provenance import sha256_file


def source(email="person@example.com", salutation="Trey", active=True, act_id="one", **updates):
    return {"norm_email": email, "raw_salutation": salutation,
            "record_active": active, "source_record_id": act_id, **updates}


def choose(records=(), roster_names=("Jim Example",), first="JAMES", middle="", used="",
           last="Example", email="person@example.com"):
    return choose_contact_greeting(
        email=email, act_primary_by_email=build_act_primary_email_index(records),
        roster_names=roster_names,
        sec={"first":first, "middle":middle, "used":used, "last":last},
        email_unique=True, authoritative_domain=True)


class PreferredNameTests(unittest.TestCase):
    def test_primary_unique_salutation_needs_no_sec_validation(self):
        decision = choose([source(salutation="Trey")], roster_names=("James Wolslager",),
                          first="JAMES", last="Wolslager")
        self.assertEqual("Trey", decision.greeting)
        self.assertEqual(ACT_PRIMARY_SOURCE, decision.source)
        self.assertEqual("one", decision.act_contact_id)
        self.assertRegex(decision.evidence_hash, r"^[0-9a-f]{64}$")

    def test_alternate_email_cannot_supply_chads_greeting_to_heather(self):
        record = source(email="chad@example.com", salutation="Chad",
                        raw_alt_emails_json='["heather@example.com"]')
        decision = choose([record], email="heather@example.com",
                          roster_names=("Heather Winfield",), first="Heather", last="Winfield")
        self.assertEqual("Heather", decision.greeting)
        self.assertEqual(ROSTER_SOURCE, decision.source)
        self.assertEqual("", decision.act_contact_id)

    def test_shared_primary_and_inactive_primary_do_not_supply_salutations(self):
        for records in [
            [source(salutation="Chad"), source(salutation="Veronica", act_id="two")],
            [source(salutation="Chad", active=False)],
            [source(salutation="Chad"), source(salutation="Veronica", active=False, act_id="two")],
        ]:
            with self.subTest(records=records):
                decision = choose(records)
                self.assertEqual("Jim", decision.greeting)
                self.assertEqual(ROSTER_SOURCE, decision.source)

    def test_act_cleanup_parentheticals_initials_and_casing(self):
        for raw, expected in [("Lawrence (Larry)","Larry"), ("Stephanus (Willy)","Willy"),
                              ("Kelly (male)","Kelly"), ("TJ","TJ"), ("ANDREW","Andrew"),
                              ("A.J.","A.J."), ("J.P.","J.P."), ("JR","JR")]:
            with self.subTest(raw=raw):
                decision = choose([source(salutation=raw)])
                self.assertEqual(expected, decision.greeting)
                self.assertEqual(ACT_PRIMARY_SOURCE, decision.source)

    def test_unusable_or_invalid_act_greeting_falls_through_to_roster(self):
        for raw in ("Mr.","Mr. Porter","Mr.Gary","retired","RETIRED","C.","W.",
                    "Jr","Jr.","A.j.","team", "assistant", "inactive", "deceased", "none"):
            with self.subTest(raw=raw):
                decision = choose([source(salutation=raw)])
                self.assertEqual("Jim", decision.greeting)
                self.assertEqual(ROSTER_SOURCE, decision.source)

    def test_roster_jim_beats_sec_james_and_initial_led_roster_uses_scott(self):
        self.assertEqual(("Jim", ROSTER_SOURCE),
                         (choose().greeting, choose().source))
        decision = choose(roster_names=("J. Scott Example",), first="JAMES", middle="SCOTT")
        self.assertEqual("Scott", decision.greeting)
        self.assertEqual(ROSTER_SOURCE, decision.source)
        decision = choose(roster_names=("J. Example",), first="JAMES")
        self.assertEqual("James", decision.greeting)
        self.assertEqual(SEC_SOURCE, decision.source)

    def test_roster_email_nickname_is_used_only_after_act_primary(self):
        roster=choose(email="jim.smith@firm.example",roster_names=("James Smith",),
                      first="JAMES",last="Smith")
        self.assertEqual("Jim",roster.greeting)
        self.assertEqual(ROSTER_SOURCE,roster.source)
        self.assertEqual("roster_email_nickname_refinement",roster.reason)
        primary=choose([source(email="jim.smith@firm.example",salutation="Jamie")],
                       email="jim.smith@firm.example",roster_names=("James Smith",),
                       first="JAMES",last="Smith")
        self.assertEqual("Jamie",primary.greeting)
        self.assertEqual(ACT_PRIMARY_SOURCE,primary.source)

    def test_roster_email_nickname_preserves_jr_source_restriction(self):
        decision=choose(email="jr.smith@firm.example",roster_names=("John Smith",),
                        first="JOHN",used="JR",last="Smith")
        self.assertNotEqual("JR",decision.greeting)
        self.assertEqual(ROSTER_SOURCE,decision.source)

    def test_sec_fallback_skips_junk_and_has_a_source(self):
        for used in ("CHRISTOPHER", "(III)", "R.D.", "L"):
            with self.subTest(used=used):
                decision = choose(roster_names=(), first="CHRISTOPHER ANDREW", used=used)
                self.assertEqual("Christopher", decision.greeting)
                self.assertEqual(SEC_SOURCE, decision.source)
        decision = choose(roster_names=(), first="J", used="(III)")
        self.assertEqual("", decision.greeting)
        self.assertEqual("no_usable_greeting", decision.source)

    def test_invalid_act_or_sec_compound_keeps_a_usable_filed_fallback(self):
        decision = choose([source(salutation="Dee Dee")], roster_names=(),
                          first="DEIRDRE", middle="DENISE", used="DEE DEE")
        self.assertEqual("Deirdre", decision.greeting)
        self.assertEqual(SEC_SOURCE, decision.source)
        decision = choose(roster_names=("V Carbo",), first="V", middle="J",
                          used="V. JAMES", last="Carbo")
        self.assertEqual("James", decision.greeting)
        self.assertEqual(SEC_SOURCE, decision.source)
        decision = choose(roster_names=(), first="CHRISTOPHER", used="V. JAMES")
        self.assertEqual("Christopher", decision.greeting)

    def test_jr_is_only_accepted_from_unique_active_act_primary(self):
        self.assertEqual("JR", choose([source(salutation="JR")]).greeting)
        for records in ([], [source(salutation="JR",active=False)]):
            decision = choose(records, roster_names=("JR Example",), first="JOHN", used="JR")
            self.assertEqual("John", decision.greeting)
            self.assertEqual(SEC_SOURCE, decision.source)
        for text in ("Jr", "Jr."):
            self.assertEqual("", clean_act_salutation(text))

    def test_roster_rows_must_be_accepted_and_match_the_sending_email(self):
        group = pd.DataFrame([
            {"name":"Jim Example", "email":"person@example.com", "source":"Roster", "tier":"high", "match_score":1.0},
            {"name":"Chad Example", "email":"chad@example.com", "source":"Roster", "tier":"high", "match_score":1.0},
            {"name":"Other Example", "email":"person@example.com", "source":"Roster", "tier":"review", "match_score":1.0},
            {"name":"ACT Name", "email":"person@example.com", "source":"CRM", "tier":"confirmed", "match_score":1.0},
        ])
        before = group.copy(deep=True)
        self.assertEqual(["Jim Example"], roster_names_for_contact(group, "person@example.com"))
        pd.testing.assert_frame_equal(before, group)

    def test_greeting_selector_never_mutates_identity_or_routing_inputs(self):
        records = [source(raw_claimed_crd="123", can_email=False, identity_status="review")]
        before = copy.deepcopy(records)
        decision = choose(records)
        self.assertEqual("Trey", decision.greeting)
        self.assertEqual(before, records)
        self.assertFalse(hasattr(decision, "can_email"))
        self.assertFalse(hasattr(decision, "advisor_crd"))

    def test_loader_uses_the_hash_bound_snapshot_and_includes_inactive_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            act = root / "act_contacts_snapshot.json"
            act.write_text("[]", encoding="utf-8")
            frame = pd.DataFrame([source(), source(active=False, act_id="two")])
            path = root / "act_source_records.parquet"
            frame.to_parquet(path, index=False)
            core = {"actSource":{"file":act.name,"sha256":sha256_file(act),"rows":2},
                    "outputs":{path.name:{"sha256":sha256_file(path),"rows":2}}}
            manifest = root / "identity_manifest.json"
            manifest.write_text(json.dumps({**core,"contentHash":content_hash(core)}), encoding="utf-8")
            index = load_act_primary_email_index(root, act)
            self.assertEqual(2, len(index["person@example.com"]))
            path.write_bytes(b"modified")
            with self.assertRaisesRegex(RuntimeError,"stale or modified"):
                load_act_primary_email_index(root, act)

    def test_display_is_transparent_and_idempotent(self):
        shown = preferred_display_name("Christopher Tolman", "Chris")
        self.assertEqual("Christopher (Chris) Tolman", shown)
        self.assertEqual(shown, preferred_display_name(shown, "Chris"))
        self.assertEqual("Chris Tolman",
                         preferred_display_name("Chris Tolman", "Chris"))
        self.assertEqual("Chris Tolman",
                         preferred_display_name("Chris Tolman", "Christopher"))
        self.assertEqual("Christopher Tolman",
                         preferred_display_name("Christopher Tolman", "Hi Chris"))

    def test_desktop_and_field_builds_use_prepared_presentation_name(self):
        desktop = (ROOT / "webapp" / "app.js").read_text(encoding="utf-8")
        field_build = (ROOT / "src" / "build_field_tiles.py").read_text(
            encoding="utf-8")
        self.assertIn("function advisorDisplayName(", desktop)
        self.assertIn(
            'return (c && c.pn) || fallback || (c && c.n) || "";',
            desktop)
        self.assertIn("name: advisorDisplayName(id, p && p.n)", desktop)
        self.assertIn("const shownName = advisorDisplayName(row[0], row[1])",
                      desktop)
        self.assertIn("out.push({ crd: String(id), name: advisorDisplayName(id)",
                      desktop)
        self.assertIn(
            "const label = advisorDisplayName(entry.advisorCrd, fallback)",
            desktop)
        self.assertIn('shown_name = c.get("pn") or formal_name', field_build)
        self.assertIn('return contact.get("pn") or formal', field_build)
        self.assertNotIn("preferred_display_name(", field_build)
        self.assertIn("shown_member_name(crd)", field_build)
        contact_build = (ROOT / "src" / "build_contacts.py").read_text(
            encoding="utf-8")
        self.assertIn("presented and presented != canonical", contact_build)
        self.assertIn("choose_contact_greeting(", contact_build)
        self.assertNotIn("load_greeting_overlays(", contact_build)
        self.assertIn(
            'authoritative_domain=bool(row.get("authoritative_domain", False))',
            contact_build)
        self.assertNotIn("authoritative_domain=True", contact_build)
        self.assertNotIn(
            'entry["pih"] = roster_decision.evidence_hash', contact_build)

    def test_registry_audit_rejects_unusable_merge_names(self):
        audit = (ROOT / "src" / "audit.py").read_text(encoding="utf-8")
        self.assertIn('row.get("greetingName")', audit)
        self.assertIn("invalid_greetings", audit)
        self.assertIn("if not valid_greeting(greeting, source=", audit)
        self.assertIn('row.get("lastName")', audit)
        self.assertIn("invalid_last_names", audit)
        for real_name in ("JD", "Vi", "Ea"):
            self.assertTrue(valid_greeting(real_name))
        for unsafe in ("J", "III", "CFP", "Dr."):
            self.assertFalse(valid_greeting(unsafe))


if __name__ == "__main__":
    unittest.main()
