from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from roster_continuity import canonical_value,compatible_names,bridge_keys,email_owner_names_agree


class ContinuityTests(unittest.TestCase):
    def test_renamed_profiles_and_nickname_aliases_are_not_email_swaps(self):
        old={'a':{'name':{'matt werner'}}}
        new={'b':{'name':{'matt werner'}},'c':{'name':{'matthew werner'}}}
        self.assertTrue(email_owner_names_agree(old,new,{'a'},{'b','c'}))
        new['c']['name']={'alice werner'}
        self.assertFalse(email_owner_names_agree(old,new,{'a'},{'b','c'}))
        self.assertFalse(email_owner_names_agree({'a':{'name':set()}},new,{'a'},{'b'}))

    def test_credentials_and_tracking_are_not_person_changes(self):
        self.assertEqual('alice smith',canonical_value('name','alice smith, cfp, cpfatm'))
        self.assertEqual(canonical_value('profile','http://example.com/alice/?utm_source=x'),
                         canonical_value('profile','https://example.com/alice'))
        self.assertNotEqual(canonical_value('profile','https://example.com/profile?id=1'),
                            canonical_value('profile','https://example.com/profile?id=2'))

    def test_address_normalization_retains_units_and_direction(self):
        self.assertEqual(canonical_value('address','100 Main Street, Suite 4'),
                         canonical_value('address','100 Main St Ste 4'))
        for other in ['100 Main St Ste 5','100 Main St NE Ste 4','101 Main St Ste 4']:
            self.assertNotEqual(canonical_value('address','100 Main St Ste 4'),canonical_value('address',other))
        self.assertEqual(canonical_value('address','One Liberty Square 13th Floor Suite 4'),
                         canonical_value('address','1 Liberty Sq Floor 13 #4'))
        self.assertNotEqual(canonical_value('address','40 Wall Street 56th Floor'),
                            canonical_value('address','40 Wall St Floor 60'))

    def test_only_compatible_names_bridge(self):
        self.assertTrue(compatible_names({'christopher smith'},{'chris smith'}))
        self.assertFalse(compatible_names({'alice smith'},{'bob smith'}))
        self.assertFalse(compatible_names({'person 0'},{'person 1'}))
        self.assertFalse(compatible_names({'john james smith'},{'john michael smith'}))
        self.assertFalse(compatible_names({'john smith sr'},{'john smith jr'}))

    def test_key_namespace_change_needs_unique_email_and_name(self):
        a='provider:leid:1';b='provider:agent_number:2'
        old={a:{'name':{'alice smith'}}};new={b:{'name':{'alice smith'}}}
        current,_,count=bridge_keys(old,new,{'a@example.com':{a}},{'a@example.com':{b}},'northwestern_mutual')
        self.assertEqual({a},set(current));self.assertEqual(1,count)
        new[b]['name']={'bob smith'}
        self.assertEqual(0,bridge_keys(old,new,{'a@example.com':{a}},{'a@example.com':{b}},'northwestern_mutual')[2])
        new[b]['name']={'alice smith'}
        self.assertEqual(0,bridge_keys(old,new,{'a@example.com':{a}},{'a@example.com':{a,b}},'northwestern_mutual')[2])


if __name__=='__main__':unittest.main()
