"""Last-good ranking identity reuse is exact, auditable and never fuzzy."""
from pathlib import Path
import sys
import unittest
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from refresh_rankings import reuse_barrons_identifiers
import refresh_core as core
import refresh_acquire as acquire
from refresh_resilience import HistoricalHold

class RankingReuseTests(unittest.TestCase):
    def setUp(self):
        self.prior={'advisor':'Example Advisor','firm':'Example Firm','location':'City','state':'GA',
            'ranking':'top1500','year':2025,'crd':'123','barrons_url':'https://www.barrons.com/advisor/finder/example'}
        self.fresh={**self.prior,'year':2026,'crd':None,'rank':2}
    def test_exact_prior_identity_is_reused_without_mutating_capture(self):
        rows,audit=reuse_barrons_identifiers([self.fresh],[self.prior])
        self.assertEqual(rows[0]['crd'],'123');self.assertEqual(len(audit),1)
        self.assertEqual(rows[0]['crd_origin'],'prior_exact_publisher_record')
        self.assertEqual(len(rows[0]['crd_prior_record_sha256']),64)
        self.assertIsNone(self.fresh['crd']);self.assertNotIn('crd_origin',self.prior)
    def test_no_changed_person_firm_location_cohort_or_future_evidence(self):
        for patch in [{'advisor':'Different Advisor'},{'firm':'Different Firm'},{'location':'Other City'},
                      {'state':'NY'},{'ranking':'top100'},{'year':2024},{'year':None},
                      {'barrons_url':'https://other.example/advisor/finder/example'}]:
            with self.subTest(patch=patch):
                self.assertEqual(reuse_barrons_identifiers([{**self.fresh,**patch}],[self.prior])[1],[])
    def test_conflicting_prior_id_blocks_reuse_even_in_another_category(self):
        other={**self.prior,'crd':'456','ranking':'top100'}
        self.assertEqual(reuse_barrons_identifiers([self.fresh],[self.prior,other])[1],[])
    def test_fresh_assertion_is_never_overwritten(self):
        rows,audit=reuse_barrons_identifiers([{**self.fresh,'crd':'456'}],[self.prior])
        self.assertEqual(rows[0]['crd'],'456');self.assertFalse(audit)

    def test_real_import_preserves_raw_capture_and_binds_prior_proof(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);stage=root/'stage';scratch=root/'scratch';scratch.mkdir()
            prior=stage/'data/raw/barrons_rankings.json';core.atomic_json(prior,[self.prior])
            source=root/'incoming.json';core.atomic_json(source,[self.fresh])
            before=source.read_bytes();old=prior.read_bytes()
            products,details=acquire._prepare(stage,'barrons',source,scratch,None,'2026',(),None)
            self.assertEqual(core.read_json(products[0][0])[0]['crd'],'123')
            self.assertFalse(details['reusedRankingIdentifiers']['freshIdentityLookup'])
            self.assertEqual(details['historicalAcceptance']['status'],'accepted')
            self.assertIsNone(details['historicalAcceptance']['reviewDecisionSha256'])
            self.assertEqual(source.read_bytes(),before);self.assertEqual(prior.read_bytes(),old)

    def test_changed_firm_still_hits_the_normal_historical_hold(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);stage=root/'stage';scratch=root/'scratch';scratch.mkdir()
            core.atomic_json(stage/'data/raw/barrons_rankings.json',[self.prior])
            source=root/'incoming.json';core.atomic_json(source,[{**self.fresh,'firm':'Different Firm'}])
            with self.assertRaises(HistoricalHold):
                acquire._prepare(stage,'barrons',source,scratch,None,'2026',(),None)

if __name__=='__main__':unittest.main()
