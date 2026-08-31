from __future__ import annotations

import pathlib
import sys
import unittest

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from preferred_names import build_greeting_overlays, preferred_display_name
from roster_greetings import valid_greeting


def source(**updates):
    row = {
        "record_key": "act:one", "source_system": "ACT",
        "raw_claimed_crd": "",
        "raw_salutation": "Chris", "norm_email": "c.tolman@ubs.com",
        "generic_email": False, "email_claim_count": 1,
        "record_active": True,
    }
    row.update(updates)
    return row


def evidence(**updates):
    row = {
        "record_key": "act:one", "candidate_crd": "2066775",
        "candidate_tier": "high", "candidate_score": 1.0,
        "candidate_gap": 1.0, "full_surname_exact": True,
        "given_exact": False, "given_sec_used": False,
        "given_strict_nickname": True, "sec_first_name": "Christopher",
        "sec_used_first_name": "", "email_unique_in_act": True,
        "email_personal": True, "email_domain_current_agrees": True,
        "firm_current_agrees": True, "roster_email_exact": True,
        "roster_name_agrees": True, "roster_firm_current_agrees": True,
        "hard_conflicts_json": "[]", "evidence_hash": "evidence-one",
    }
    row.update(updates)
    return row


def link(**updates):
    row = {
        "record_key": "act:one", "identity_status": "unmatched",
        "decision_reason": "no_asserted_crd", "preferred_first": "Chris",
        "preferred_status": "approved_auto",
        "preferred_reason": "preferred_strict_nickname",
        "resolved_evidence_hash": "evidence-one",
    }
    row.update(updates)
    return row


def build(src=None, ev=None, lnk=None):
    return build_greeting_overlays(
        pd.DataFrame(src or [source()]), pd.DataFrame(ev or [evidence()]),
        pd.DataFrame(lnk or [link()]))


class PreferredNameTests(unittest.TestCase):
    def test_strict_nickname_overlay_is_presentation_only(self):
        overlay = build()["2066775"]
        self.assertEqual("c.tolman@ubs.com", overlay["email"])
        self.assertEqual("Chris", overlay["greeting"])
        self.assertEqual("act_primary_email_overlay_v1", overlay["source"])
        self.assertNotIn("actContactId", overlay)
        self.assertNotIn("sourceRecordId", overlay)

    def test_sec_used_first_name_is_accepted(self):
        overlay = build(
            src=[source(raw_salutation="Scott")],
            ev=[evidence(given_strict_nickname=False, given_sec_used=True,
                         sec_used_first_name="Scott")],
            lnk=[link(preferred_first="Scott",
                      preferred_reason="preferred_exact")])
        self.assertEqual("Scott", overlay["2066775"]["greeting"])

    def test_legal_first_name_does_not_replace_a_roster_used_name(self):
        overlay = build(
            ev=[evidence(given_strict_nickname=False, given_exact=True)],
            lnk=[link(preferred_first="Christopher",
                      preferred_reason="preferred_exact")])
        self.assertEqual({}, overlay)

    def test_primary_email_and_every_identity_gate_are_required(self):
        blocked = [
            (source(raw_claimed_crd="2066775"), evidence(), link()),
            (source(email_claim_count=2), evidence(), link()),
            (source(norm_email="office@ubs.com", generic_email=True),
             evidence(), link()),
            (source(), evidence(roster_email_exact=False), link()),
            (source(), evidence(roster_firm_current_agrees=False), link()),
            (source(), evidence(email_domain_current_agrees=False), link()),
            (source(), evidence(hard_conflicts_json='["firm_conflict"]'), link()),
            (source(), evidence(candidate_gap=0.24), link()),
            (source(), evidence(), link(preferred_status="review")),
            (source(), evidence(), link(resolved_evidence_hash="stale")),
        ]
        for src, ev, lnk in blocked:
            with self.subTest(src=src, ev=ev, link=lnk):
                self.assertEqual({}, build([src], [ev], [lnk]))

    def test_duplicate_crd_or_email_rejects_every_overlay(self):
        src2 = source(record_key="act:two", norm_email="chris@ubs.com")
        ev2 = evidence(record_key="act:two", evidence_hash="evidence-two")
        link2 = link(record_key="act:two",
                     resolved_evidence_hash="evidence-two")
        self.assertEqual({}, build(
            [source(), src2], [evidence(), ev2], [link(), link2]))

        ev2["candidate_crd"] = "999"
        src2["norm_email"] = "c.tolman@ubs.com"
        self.assertEqual({}, build(
            [source(), src2], [evidence(), ev2], [link(), link2]))

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
        self.assertIn("if not valid_greeting(greeting)", audit)
        self.assertIn('row.get("lastName")', audit)
        self.assertIn("invalid_last_names", audit)
        for real_name in ("JD", "Vi", "Ea"):
            self.assertTrue(valid_greeting(real_name))
        for unsafe in ("J", "III", "CFP", "Dr."):
            self.assertFalse(valid_greeting(unsafe))


if __name__ == "__main__":
    unittest.main()
