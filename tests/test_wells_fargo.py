import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wells_fargo_async import parse_page


class WellsFargoPageTests(unittest.TestCase):
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
        self.assertEqual(2, len(rows))
        self.assertEqual(
            [("Marc Johnson", "marc@example.com", "5854548227"),
             ("Gene Richardson", "gene@example.com", "5854548225")],
            [(r["name"], r["emails"], r["phone_numbers"]) for r in rows],
        )
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
        self.assertEqual("Example Wealth Group", rows[0]["name"])


if __name__ == "__main__":
    unittest.main()
