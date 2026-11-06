"""Branch staff addresses must not win over a name-supported advisor mailbox."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from edward_jones_email import select_email


class EdwardJonesMailboxTests(unittest.TestCase):
    def select(self, name, *locals):
        return select_email(name, [value + '@edwardjones.com' for value in locals])

    def test_walter_numeric_name_wins_over_other_smith(self):
        result = self.select('Walter Smith', "meg'an.smith", 'torrey.mason', 'walter.smith2')
        self.assertEqual(result.email, 'walter.smith2@edwardjones.com')

    def test_jacob_numeric_name_wins_over_owen(self):
        self.assertEqual(self.select('Jacob Newlove', 'owen.newlove', 'jacob.newlove2').email,
                         'jacob.newlove2@edwardjones.com')

    def test_hyphenated_and_apostrophe_surnames(self):
        for name, local in [('Cheryl McCormick-Hann', 'cheryl.mccormick-hann'),
                            ("Bryan O'Dell", 'bryan.odell'),
                            ('Jesus Vega-Cerda', 'jesus.vega-cerda')]:
            with self.subTest(name=name):
                self.assertEqual(self.select(name, 'other.person', local).email,
                                 local + '@edwardjones.com')

    def test_known_nicknames_and_published_parenthetical(self):
        for name, local in [('Deb Hall-Fisher', 'debbie.hall-fisher'),
                            ('Jim Zawacki', 'james.zawacki'),
                            ('Robert (Bo) Ladyman', 'bo.ladyman')]:
            self.assertEqual(self.select(name, 'other.person', local).email,
                             local + '@edwardjones.com')

    def test_exact_compacted_full_source_name(self):
        self.assertEqual(self.select('Michele Pearson Tucker', 'michele.pearsontucker').email,
                         'michele.pearsontucker@edwardjones.com')
        self.assertEqual(self.select('Kareem Johnson', 'kareemjohnson').email,
                         'kareemjohnson@edwardjones.com')

    def test_literal_name_precedes_nickname_not_list_order(self):
        for locals in [('michael.anderson2', 'mike.anderson'),
                       ('mike.anderson', 'michael.anderson2')]:
            self.assertEqual(self.select('Mike Anderson', *locals).email,
                             'mike.anderson@edwardjones.com')

    def test_equally_name_supported_numeric_mailboxes_require_review(self):
        for name, local in [('Donald Paul', 'donald.paul'), ('Daniel Heppler', 'daniel.heppler')]:
            result = self.select(name, local, local + '2')
            self.assertFalse(result.email)
            self.assertEqual(result.reason, 'ambiguous_literal_full_name')
            self.assertEqual(len(result.matched), 2)

    def test_single_staff_or_changed_surname_is_not_ownership_proof(self):
        for name, local in [('Charity Simmons', 'charity.abercrombie'),
                            ('Jacob Newlove', 'owen.newlove'),
                            ('Taylor Jones', 'taylor.johnson')]:
            self.assertFalse(self.select(name, local).email)

    def test_initial_is_supported_without_expanding_to_unrelated_given(self):
        self.assertEqual(self.select('James Mitchell', 'j.mitchell', 'jane.mitchell').email,
                         'j.mitchell@edwardjones.com')

    def test_middle_calling_name_after_initial(self):
        self.assertEqual(self.select('A. Dwight Smith', 'dwight.smith').email,
                         'dwight.smith@edwardjones.com')

    def test_middle_initials_in_native_mailbox_are_not_lost(self):
        for name, local in [('Rhonda Jones', 'rhonda.l.jones'),
                            ('Daniel Howard', 'v.daniel.howard'),
                            ('Jim Brown', 'jim.a.brown')]:
            with self.subTest(name=name):
                self.assertEqual(self.select(name, local, 'other.person').email,
                                 local + '@edwardjones.com')

    def test_unseparated_initial_needs_explicit_name_or_profile_evidence(self):
        self.assertFalse(self.select('John Davis', 'johnt.davis').email)
        self.assertEqual(self.select('John T Davis', 'johnt.davis').email,
                         'johnt.davis@edwardjones.com')
        self.assertEqual(select_email('John Davis', ['johnt.davis@edwardjones.com'],
            '/us-en/financial-advisor/johnt-davis').email, 'johnt.davis@edwardjones.com')

    def test_different_given_names_are_not_unseparated_initials(self):
        self.assertEqual(self.select('Joe Smith', 'joel.smith', 'joe.smith2').email,
                         'joe.smith2@edwardjones.com')
        self.assertFalse(self.select('Tim Smith', 'timo.smith').email)

    def test_invalid_profile_does_not_crash_an_entire_roster(self):
        self.assertEqual(select_email('Jane Smith', ['other.person@edwardjones.com'],
            'https://[bad-host').reason, 'invalid_source_profile')

    def test_existing_project_nickname_groups_remain_supported(self):
        for name, local in [('Joshua Tenery', 'josh.tenery'),
                            ('Josh Little', 'joshua.little'),
                            ('Russ Willemsen', 'russell.willemsen')]:
            self.assertEqual(self.select(name, local).email, local + '@edwardjones.com')

    def test_multiple_middle_initial_candidates_remain_ambiguous(self):
        self.assertFalse(self.select('John Smith', 'john.a.smith', 'john.b.smith').email)

    def test_junior_name_never_selects_seniors_conflicting_given(self):
        self.assertFalse(self.select('Conner Chitty', 'charles.chitty', 'sharon.chitty').email)
        self.assertFalse(self.select('Benjamin Wilkes', 'benjamin.wilkesiii').email)

    def test_empty_invalid_and_outside_firm_addresses(self):
        self.assertFalse(select_email('', ['jane.smith@edwardjones.com']).email)
        self.assertFalse(select_email('Jane Smith', ['jane.smith@other.example']).email)
        self.assertFalse(select_email('Jane Smith', ['Jane <jane.smith@edwardjones.com>']).email)
        self.assertFalse(select_email('Jane Smith', []).email)

    def test_duplicate_addresses_and_case_do_not_invent_ambiguity(self):
        self.assertEqual(select_email('Jane Smith', ['JANE.SMITH@EDWARDJONES.COM',
            'jane.smith@edwardjones.com']).email, 'jane.smith@edwardjones.com')

    def test_native_personal_profile_can_corroborate_calling_name(self):
        for name, route, local in [('Price Wilkes', 'richard-wilkes', 'richard.wilkes'),
                                   ('Riyah Ward', 'kariyah-ward', 'kariyah.ward'),
                                   ('Tiffany Little', 'tiffany-dugger', 'tiffany.dugger'),
                                   ('Jeff Miller', 'jmiller', 'jmiller')]:
            result = select_email(name, [local + '@edwardjones.com'],
                                  '/us-en/financial-advisor/' + route)
            self.assertEqual(result.email, local + '@edwardjones.com')
            self.assertEqual(result.reason, 'published_personal_profile_name')

    def test_profile_evidence_does_not_resolve_full_name_ambiguity(self):
        result = select_email('Daniel Heppler', ['daniel.heppler@edwardjones.com',
            'daniel.heppler2@edwardjones.com'], '/us-en/financial-advisor/daniel-heppler2')
        self.assertFalse(result.email)
        self.assertEqual(result.reason, 'ambiguous_literal_full_name')

    def test_profile_never_supplies_unrelated_names_or_unsafe_origins(self):
        for route in ['/us-en/financial-advisor/alex-brown',
                      'https://other.example/us-en/financial-advisor/alex-brown',
                      '//other.example/us-en/financial-advisor/alex-brown',
                      '/us-en/search/financial-advisor/results']:
            self.assertFalse(select_email('Jane Smith', ['alex.brown@edwardjones.com'], route).email)

    def test_jr_as_a_given_name_is_not_stripped_as_a_suffix(self):
        for last in ['Kaster', 'Frakes', 'Whitaker']:
            local = 'jr.' + last.lower()
            self.assertEqual(self.select('JR ' + last, local).email,
                             local + '@edwardjones.com')

    def test_reverse_order_needs_exact_published_profile_evidence(self):
        self.assertFalse(self.select('Gabe Pettit', 'pettit.gabe').email)
        self.assertEqual(select_email('Gabe Pettit', ['pettit.gabe@edwardjones.com'],
            '/us-en/financial-advisor/pettit-gabe').email, 'pettit.gabe@edwardjones.com')

    def test_compound_formal_names_need_same_profile_and_both_name_parts(self):
        self.assertEqual(select_email('Andres Moreno', ['henryandres.morenoalzate@edwardjones.com'],
            '/us-en/financial-advisor/henryandres-morenoalzate').email,
            'henryandres.morenoalzate@edwardjones.com')
        self.assertFalse(select_email('Joe Smith', ['joella.smithson@edwardjones.com'],
            '/us-en/financial-advisor/joella-smithson').email)
