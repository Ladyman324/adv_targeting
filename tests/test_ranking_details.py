"""Published ranking financial details retain their units and uncertainty."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from parse_barrons import _number,_detail

class RankingDetailsTests(unittest.TestCase):
    def test_numbers_do_not_collapse_ranges_or_negative_values(self):
        self.assertEqual(_number('$1,250.50'),1250.5)
        self.assertEqual(_number('0.5'),.5)
        for value in ['5-10','5 to 10','-5','1e3','12,34','N','9'*400,None]:
            self.assertIsNone(_number(value),msg=str(value)[:40])

    def test_units_are_normalized_not_assumed_from_field_name(self):
        values=_detail({'detail':{'Team Assets ($mil)':'12','Typical Account ($bil)':'0.005'}})
        self.assertEqual(values['team_assets_usd'],12_000_000)
        self.assertEqual(values['team_assets_bil'],.012)
        self.assertEqual(values['typical_account_usd'],5_000_000)
        self.assertEqual(values['typical_account_mil'],5)

    def test_unknown_units_remain_unknown(self):
        values=_detail({'detail':{'Team Assets (unknown units)':'12'}})
        self.assertIsNone(values['team_assets_usd'])

if __name__=='__main__':unittest.main()
