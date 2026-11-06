import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wells_fargo_async import parse_page,related_team_urls,_contact_link
from bs4 import BeautifulSoup


class WellsFargoPageTests(unittest.TestCase):
    def test_all_person_card_phones_preserved_without_fax_or_colleague(self):
        html='''<div class="card--profile"><div class="card--contact-info">
        <p>Phone <a href="tel:9129213434">Local</a></p>
        <p>Toll Free <a href="tel:8009290838">Toll free</a></p>
        <p>Phone <a href="TEL:+1-912-921-3434">Same local</a></p>
        <p>Fax <a href="tel:9129276313">Fax</a></p>
        <p><a href="tel:123">Invalid</a></p></div></div>
        <div class="card--profile"><div class="card--contact-info"><a href="tel:2125559999">Colleague</a></div></div>'''
        card=BeautifulSoup(html,'html.parser').select_one('.card--profile')
        self.assertEqual('9129213434; 8009290838',_contact_link(card,'tel'))

    def test_business_card_phone_fallback_when_main_contact_area_has_only_email(self):
        html='''<div class="biz--card"><h2 class="name">Jane Smith</h2><div class="card--contact-info"><a href="mailto:jane@example.com">Email</a></div><div class="container--contact"><p>Phone <a href="tel:2125551111">Phone</a></p><p>Toll Free <a href="tel:8005551111">Toll Free</a></p></div></div>'''
        self.assertEqual('2125551111; 8005551111',_contact_link(BeautifulSoup(html,'html.parser').div,'tel'))

    def test_team_cards_keep_each_persons_contact_details_together(self):
        html = """
        <title>Financial Advisors | Cornerstone Wealth Management, Rochester, NY</title>
        <div class="address">1640 Penfield Road Rochester, NY 14625</div>
        <section class="section--team">
          <div class="card--profile">
            <div class="container--card-title">
              <h2 class="name"><span class="name-data">Marc Johnson</span></h2>
              <p class="title">President</p>
              <div class="card--contact-info">
                <a href="mailto:marc@example.com">Email me</a>
                <a href="tel:5854548227">Call me</a>
              </div>
            </div>
          </div>
          <div class="card--profile">
            <div class="container--card-title">
              <h2 class="name"><span class="name-data">Gene Richardson</span></h2>
              <p class="title">Managing Partner</p>
              <div class="card--contact-info">
                <a href="mailto:gene@example.com">Email me</a>
                <a href="tel:5854548225">Call me</a>
              </div>
            </div>
          </div>
        </section>
        <a href="mailto:office@example.com">General inquiries</a>
        """
        rows = parse_page(html, "https://fa.wellsfargoadvisors.com/cornerstone/")
        self.assertEqual(3, len(rows))
        people=[row for row in rows if row['record_type']=='person']
        self.assertEqual(
            [("Marc Johnson", "marc@example.com", "5854548227"),
             ("Gene Richardson", "gene@example.com", "5854548225")],
            [(r["name"], r["emails"], r["phone_numbers"]) for r in people],
        )
        self.assertEqual('office@example.com',next(r for r in rows if r['record_type']=='office_or_unresolved')['emails'])
        self.assertTrue(all(r["team_name"] == "Cornerstone Wealth Management" for r in rows))
        self.assertTrue(all(r["address"] == "1640 Penfield Road Rochester, NY 14625" for r in rows))

    def test_individual_profile_still_returns_one_row(self):
        html = """
        <div class="page--title"><h1>Kim Coggins</h1><p class="title">Financial Advisor</p></div>
        <a href="mailto:kim@example.com">Email Kim</a>
        <a href="tel:4043654981">Call Kim</a>
        """
        rows = parse_page(html, "https://fa.wellsfargoadvisors.com/kim-coggins/")
        self.assertEqual(1, len(rows))
        self.assertEqual("Kim Coggins", rows[0]["name"])
        self.assertEqual("kim@example.com", rows[0]["emails"])
        self.assertEqual("", rows[0]["team_url"])

    def test_personal_title_beats_marketing_headline_and_teammate(self):
        html = """
        <title>Stanley Janowiak, Financial Advisor | Wells Fargo Advisors</title>
        <div class="page--title"><h1>Working with Us</h1></div>
        <h2 class="name">Jessica Giuffrida</h2>
        <a href="mailto:stanley.janowiak@wellsfargoadvisors.com">Email</a>
        """
        rows = parse_page(html, "https://fa.wellsfargoadvisors.com/stanley-janowiak/")
        self.assertEqual("Stanley Janowiak", rows[0]["name"])

    def test_person_heading_beats_team_page_title(self):
        html = """
        <title>One Lafayette Wealth Management Group | Palm Beach, FL</title>
        <h1>One Lafayette Wealth Management Group</h1>
        <h2 class="name">Christopher Jewell</h2>
        <a href="mailto:chris.jewell@wellsfargo.com">Email</a>
        """
        rows = parse_page(html, "https://fa.wellsfargoadvisors.com/chris-jewell/")
        self.assertEqual("Christopher Jewell", rows[0]["name"])

    def test_multiple_unscoped_person_headings_are_not_one_contact(self):
        html = """
        <title>Example Wealth Group | Wells Fargo Advisors</title>
        <h1>Example Wealth Group</h1>
        <h2 class="name">Alice Smith</h2>
        <h2 class="name">Bob Jones</h2>
        <a href="mailto:alice.smith@example.com">Email Alice</a>
        <a href="mailto:bob.jones@example.com">Email Bob</a>
        """
        rows = parse_page(html, "https://fa.wellsfargoadvisors.com/example/")
        self.assertEqual("", rows[0]["name"])
        self.assertEqual('office_or_unresolved',rows[0]['record_type'])

    def test_marketing_headline_is_not_a_person_but_office_contacts_are_retained(self):
        html='''<title>Home - Example Wealth Group | Wells Fargo Advisors</title>
        <h1>Personalized Advice. Thoughtful Planning. Lasting Relationships.</h1>
        <script type="application/ld+json">{"@type":"LocalBusiness","name":"Example Wealth Group","address":"100 Main St, Atlanta, GA 30309"}</script>
        <a href="mailto:alice.smith@example.invalid">Message our office</a>'''
        row=parse_page(html,'https://fa.wellsfargoadvisors.com/example/')[0]
        self.assertEqual('',row['name']);self.assertEqual('Example Wealth Group',row['team_name'])
        self.assertEqual('alice.smith@example.invalid',row['emails'])
        self.assertEqual('100 Main St, Atlanta, GA 30309',row['address'])

    def test_owner_contacts_do_not_include_colleagues_card_contacts(self):
        html='''<title>Alice Smith, Financial Advisor | Wells Fargo Advisors</title>
        <a href="mailto:alice.smith@example.invalid">Alice</a><section class="section--team">
        <div class="card--profile"><div class="container--card-title"><h2 class="name"><span class="name-data">Bob Jones</span></h2>
        <div class="card--contact-info"><a href="mailto:bob.jones@example.invalid">Bob</a></div></div></div></section>'''
        rows=parse_page(html,'https://fa.wellsfargoadvisors.com/alice-smith/')
        self.assertEqual({'Alice Smith','Bob Jones'},{r['name'] for r in rows})
        self.assertEqual('alice.smith@example.invalid',next(r for r in rows if r['name']=='Alice Smith')['emails'])

    def test_published_team_links_stay_within_same_firm_site_and_practice(self):
        html='''<a href="our-team.htm">Our team</a><a href="https://evil.example/team">Our team</a>
        <a href="/other-practice/team.htm">Our people</a>'''
        self.assertEqual(['https://fa.wellsfargoadvisors.com/example/our-team.htm'],
            related_team_urls(html,'https://fa.wellsfargoadvisors.com/example/'))

    def test_short_slogan_without_personal_mailbox_corroboration_is_not_a_name(self):
        html='<h1>Proven Experience</h1><a href="mailto:jane.smith@example.invalid">Contact</a>'
        row=parse_page(html,'https://fa.wellsfargoadvisors.com/example/')[0]
        self.assertEqual('',row['name']);self.assertEqual('office_or_unresolved',row['record_type'])

    def test_line_break_role_is_not_part_of_card_person_name(self):
        html='''<section class="section--team"><div class="card--profile"><div class="container--card-title">
        <h2 class="name"><span class="name-data">Scott Smith, Esq.<br/>Executive Services Lead Financial Planning Consultant</span></h2>
        </div></div></section>'''
        rows=parse_page(html,'https://fa.wellsfargoadvisors.com/example/')
        self.assertEqual('Scott Smith',rows[0]['name'])

    def test_credentialed_and_initial_names_keep_matching_owner_contacts(self):
        cases=[('J. Douglas Riva, CLU, CFP, ChFC','douglas.riva@example.invalid','J. Douglas Riva'),
               ('Christopher Eckert, CFP, CRPS, CSRIC','christopher.eckert@example.invalid','Christopher Eckert'),
               ('William (Bill) Lemnitzer, MBA','bill.lemnitzer@example.invalid','William (Bill) Lemnitzer')]
        for heading,email,name in cases:
            with self.subTest(heading=heading):
                rows=parse_page('<h1>'+heading+'</h1><a href="mailto:'+email+'">Email</a>','https://fa.wellsfargoadvisors.com/example/')
                self.assertEqual(name,rows[0]['name']);self.assertEqual(email,rows[0]['emails'])

    def test_phone_only_credentialed_profile_keeps_name_not_office_email(self):
        html='<h1>Simon Kopel, CRPC</h1><a href="tel:2125551234">Call</a><a href="mailto:office@example.invalid">Office</a>'
        rows=parse_page(html,'https://fa.wellsfargoadvisors.com/simon-kopel/')
        person=next(r for r in rows if r['record_type']=='person')
        self.assertEqual('Simon Kopel',person['name']);self.assertEqual('',person['emails'])
        self.assertTrue(any(r['emails']=='office@example.invalid' and r['record_type']=='office_or_unresolved' for r in rows))

    def test_credentialed_heading_requires_given_and_surname_in_profile_url(self):
        rows=parse_page('<h1>Simon Kopel, CRPC</h1><a href="mailto:office@example.invalid">Contact</a>',
                        'https://fa.wellsfargoadvisors.com/other-kopel/')
        self.assertFalse(any(r['record_type']=='person' for r in rows))

    def test_topic_cards_are_not_people(self):
        html='''<section class="section--team"><div class="card--profile"><div class="container--card-title">
        <h2 class="name"><span class="name-data">Senior Executives</span></h2></div></div></section>'''
        self.assertFalse(any(r['record_type']=='person' for r in parse_page(html,'https://fa.wellsfargoadvisors.com/example/')))

    def test_bare_name_with_personal_bio_is_retained_without_contact_details(self):
        html='''<section class="section--team"><div class="card--profile"><div class="container--card-title">
        <h2 class="name"><span class="name-data">Dan Mandell</span></h2></div>
        <img class="headshot" src="media/Dave-Mandell.png"><div id="person-bio">Dan Mandel is a private wealth strategist.</div></div></section>'''
        row=parse_page(html,'https://fa.wellsfargoadvisors.com/example/')[0]
        self.assertEqual('Dan Mandell',row['name']);self.assertEqual('person',row['record_type'])
        self.assertEqual('',row['emails'])

    def test_marketing_bio_and_stock_image_do_not_prove_person(self):
        html='''<section class="section--team"><div class="card--profile"><div class="container--card-title">
        <h2 class="name"><span class="name-data">Sophisticated Resources</span></h2></div>
        <img class="headshot" src="media/AAA-SophResources.png"><div id="topic-bio">A myriad of resources helps our clients.</div></div></section>'''
        self.assertFalse(any(r['record_type']=='person' for r in parse_page(html,'https://fa.wellsfargoadvisors.com/example/')))

    def test_leading_footnote_marker_does_not_erase_staff_name(self):
        html='''<section class="section--team"><div class="card--profile"><div class="container--card-title">
        <h2 class="name"><span class="name-data">* Tim Rahr</span></h2><p class="title">Strategist</p></div></div></section>'''
        self.assertEqual('Tim Rahr',parse_page(html,'https://fa.wellsfargoadvisors.com/example/')[0]['name'])

    def test_named_portrait_retains_bare_staff_card(self):
        html='''<section class="section--team"><div class="card--profile"><div class="container--card-title">
        <h2 class="name"><span class="name-data">Arlie Kirk</span></h2></div><img class="headshot" src="media/Arlie-Kirk.png"></div></section>'''
        self.assertEqual('Arlie Kirk',parse_page(html,'https://fa.wellsfargoadvisors.com/example/')[0]['name'])

    def test_profile_url_corroboration_does_not_attach_conflicting_mailbox(self):
        html='<div class="page--title"><h1>Cam Davis</h1></div><a href="mailto:jenny.shin@example.invalid">Contact</a>'
        rows=parse_page(html,'https://fa.wellsfargoadvisors.com/cam-davis/')
        person=next(r for r in rows if r['record_type']=='person')
        self.assertEqual('Cam Davis',person['name']);self.assertEqual('',person['emails'])

    def test_compound_surnames_keep_explicit_owner_mailbox(self):
        for name,email in [("Patrick S. O'Connor, CRPC",'p.oconnor@example.invalid'),
                           ('Wasan Al Zaidi, CRPC','wasan.alzaidi@example.invalid'),
                           ('Heather Hanson-Rivas, CFP','heather.hanson@example.invalid')]:
            with self.subTest(name=name):
                rows=parse_page('<h1>'+name+'</h1><a href="mailto:'+email+'">Email</a>','https://fa.wellsfargoadvisors.com/example/')
                self.assertEqual('person',rows[0]['record_type']);self.assertEqual(email,rows[0]['emails'])

    def test_surname_first_initial_mailbox_corroborates_individual_heading(self):
        html='<h1>John Woods</h1><a href="mailto:woodsja@example.invalid">Email</a>'
        rows=parse_page(html,'https://fa.wellsfargoadvisors.com/woodsja/')
        self.assertEqual('John Woods',rows[0]['name']);self.assertEqual('woodsja@example.invalid',rows[0]['emails'])
        for mailbox in ['woodservice','woodsmj','teamwoods']:
            rows=parse_page(html.replace('woodsja',mailbox),'https://fa.wellsfargoadvisors.com/office/')
            self.assertFalse(any(r['record_type']=='person' for r in rows))

    def test_mailbox_case_variants_are_not_multiple_owner_addresses(self):
        html='<h2 class="name">Robert Iorii Jr.</h2><a href="mailto:bob.Iorii@example.invalid">Email</a><a href="mailto:bob.iorii@example.invalid">Email</a>'
        rows=parse_page(html,'https://fa.wellsfargoadvisors.com/bob-iorii/')
        self.assertEqual(1,len(rows));self.assertEqual('Robert Iorii Jr.',rows[0]['name'])
        self.assertEqual('bob.iorii@example.invalid',rows[0]['emails'])

    def test_two_people_in_one_heading_are_not_one_person(self):
        html='<h2 class="name">Albert Theis &amp; Woody Theis</h2><a href="mailto:woody.theis@example.invalid">Email</a>'
        self.assertFalse(any(r['record_type']=='person' for r in parse_page(html,'https://fa.wellsfargoadvisors.com/theis/')))

    def test_business_card_layout_preserves_scoped_person_contact_and_title(self):
        html='''<h1>Our team</h1><section class="section--team"><div class="biz--card"><div class="col-md-4"><h2 class="name"><span class="name-data">Robert Patronete</span></h2><p class="title">Financial Advisor</p><div class="container--contact"><a href="mailto:bob.patronete@example.invalid">Email</a><a href="tel:3365457100">Phone</a></div></div></div></section><footer><a href="mailto:someone.else@example.invalid">Office</a></footer>'''
        rows=parse_page(html,'https://fa.wellsfargoadvisors.com/example/')
        person=next(r for r in rows if r['record_type']=='person')
        self.assertEqual('Robert Patronete',person['name']);self.assertEqual('bob.patronete@example.invalid',person['emails'])
        self.assertEqual('3365457100',person['phone_numbers']);self.assertEqual('Financial Advisor',person['title'])
        self.assertNotIn('someone.else',person['emails'])

    def test_compound_san_surname_retains_published_address(self):
        html='<h1>Anthony San Andres</h1><a href="mailto:anthony.sanandres@example.invalid">Email</a>'
        person=parse_page(html,'https://fa.wellsfargoadvisors.com/anthony-san-andres/')[0]
        self.assertEqual('Anthony San Andres',person['name']);self.assertEqual('anthony.sanandres@example.invalid',person['emails'])

    def test_phone_and_star_role_are_separated_from_person_name(self):
        for label,expected in [('Mitchell Gravelle, CFP, MBA, 704-341-4856','Mitchell Gravelle'),
                               ('Caroline Wilhelm* Senior Custom Credit Banker','Caroline Wilhelm')]:
            html='<section class="section--team"><div class="card--profile"><div class="container--card-title"><h2 class="name"><span class="name-data">'+label+'</span></h2><p class="title">Advisor</p></div></div></section>'
            self.assertEqual(expected,parse_page(html,'https://fa.wellsfargoadvisors.com/example/')[0]['name'])


if __name__ == "__main__":
    unittest.main()
