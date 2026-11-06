"""Cross-publisher confirmation must not collapse distinct ranked namesakes."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import forbes_match

class RankingBridgeTests(unittest.TestCase):
    def test_namesake_slugs_never_choose_crd_by_file_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            people={'123':[['top100',1,None,2024,'https://www.barrons.com/advisor/finder/example-person']],
                    '456':[['top100',2,None,2026,'https://www.barrons.com/advisor/finder/example-person-2']],
                    '789':[['women',3,None,2026,'https://www.barrons.com/advisor/finder/unique-person-3'],
                           ['women',4,None,2024,'https://www.barrons.com/advisor/finder/unique-person-3']]}
            for advisors in (people,dict(reversed(list(people.items())))):
                (root/'barrons.json').write_text(json.dumps({'advisors':advisors}),encoding='utf-8')
                with patch.object(forbes_match,'WEB',root):
                    self.assertEqual(forbes_match.barrons_truth(),{'unique-person':'789'})

    def test_missing_barrons_data_is_not_confirmation(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(forbes_match,'WEB',Path(folder)):
            self.assertEqual(forbes_match.barrons_truth(),{})

if __name__=='__main__':unittest.main()
