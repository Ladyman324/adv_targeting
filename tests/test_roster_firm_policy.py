from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roster_firm_policy import authoritative_families, build_domain_policy


class RosterDomainPolicyTests(unittest.TestCase):
    def test_channel_singletons_retain_configured_raymond_james_family(self):
        rows = [
            {"email": "rja@raymondjames.com", "allowed_firm_crds": "705",
             "source_slug": "raymond_james"},
            {"email": "imd@raymondjames.com", "allowed_firm_crds": "149018",
             "source_slug": "raymond_james"},
        ]
        policy = build_domain_policy(rows)
        self.assertEqual("authoritative",
                         policy["raymondjames.com"]["status"])
        self.assertEqual(("149018", "705"),
                         authoritative_families(policy)["raymondjames.com"])

    def test_overlapping_sources_form_one_multi_entity_family(self):
        rows = [
            {"email": "a@raymondjames.com", "allowed_firm_crds": "705|149018",
             "source_slug": "raymond_james"},
            {"email": "b@raymondjames.com", "allowed_firm_crds": "705",
             "source_slug": "rj_branches"},
        ]
        policy = build_domain_policy(rows)
        self.assertEqual(("149018", "705"),
                         authoritative_families(policy)["raymondjames.com"])

    def test_disjoint_or_single_witness_domain_is_not_authoritative(self):
        rows = [
            {"email": "a@vendor.example", "allowed_firm_crds": "1"},
            {"email": "b@vendor.example", "allowed_firm_crds": "2"},
            {"email": "only@firm.example", "allowed_firm_crds": "1"},
        ]
        policy = build_domain_policy(rows)
        self.assertEqual("ambiguous", policy["vendor.example"]["status"])
        self.assertEqual("insufficient", policy["firm.example"]["status"])
        self.assertEqual({}, authoritative_families(policy))

    def test_two_disconnected_overlapping_clusters_are_ambiguous(self):
        rows = [
            {"email": "a@clusters.example", "allowed_firm_crds": "1"},
            {"email": "b@clusters.example", "allowed_firm_crds": "1|2"},
            {"email": "c@clusters.example", "allowed_firm_crds": "8"},
            {"email": "d@clusters.example", "allowed_firm_crds": "8|9"},
        ]
        policy = build_domain_policy(rows)
        self.assertEqual("ambiguous", policy["clusters.example"]["status"])
        self.assertEqual([], policy["clusters.example"]["allowedFirmCrds"])
