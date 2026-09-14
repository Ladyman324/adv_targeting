from __future__ import annotations

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from morgan_team_async import needs_retry
from morgan_stanley_async import advisor_record
from morgan_team_pages import (
    EXTRACT_BATCH_JS, audit_team_emails, blocked_batch, canonical_page_url,
    compare_rosters, email, merge_members, open_bootstrap, page_seeds,
)


def advisor(name="Devin Condron", email="devin.condron@morganstanleypwm.com"):
    return {
        "Name": name, "Primary Title": "Private Wealth Advisor",
        "Team Name": "The Condron Team", "Branch Name": "Boston PWM",
        "Office Number": "863", "Complex ID": "486",
        "Main Phone": "+16177578805", "Email": email,
        "Street Line 1": "28 State Street", "Street Line 2": "26th Fl",
        "City": "Boston", "State": "MA", "Postal Code": "02109",
        "Profile URL": "https://advisor.morganstanley.com/devin.condron",
        "Team Page URL": "https://advisor.morganstanley.com/the-condron-team",
    }


def page(members, status=200):
    return {
        "url": "https://advisor.morganstanley.com/the-condron-team",
        "finalUrl": "https://advisor.morganstanley.com/the-condron-team",
        "status": status, "team": "The Condron Team", "members": members,
        "seed": {
            "team": "The Condron Team", "branch": "Boston PWM",
            "office_number": "863", "complex_id": "486",
            "street1": "28 State Street", "street2": "26th Fl",
            "city": "Boston", "state": "MA", "postal": "02109",
        },
    }


class MorganTeamPageTests(unittest.TestCase):
    def test_only_expected_https_hosts_become_seeds(self):
        rows = [
            advisor(),
            {**advisor("Bad Host"), "Team Page URL": "https://evil.example/team"},
            {**advisor("No Team"), "Team Name": ""},
        ]
        seeds = page_seeds(rows)
        self.assertEqual(1, len(seeds))
        self.assertEqual("The Condron Team", seeds[0].team)
        self.assertEqual("Boston", seeds[0].city)
        self.assertEqual(
            "https://advisor.morganstanley.com/the-condron-team",
            canonical_page_url(
                "https://advisor.morganstanley.com//the-condron-team/?x=1#bio"))
        self.assertEqual("", canonical_page_url(
            "http://advisor.morganstanley.com/the-condron-team"))

    def test_individual_profile_is_never_used_as_a_team_page_fallback(self):
        row = advisor()
        row["Team Page URL"] = ""
        self.assertEqual([], page_seeds([row]))

    def test_directory_export_preserves_profile_and_team_urls_separately(self):
        row = advisor_record({
            "uid": "fa-1", "c_profileType": "FA", "c_pagesName": "Courtney Shane",
            "c_teamEntityName": "The Marinelli Group",
            "c_pagesURL": "https://advisor.morganstanley.com/courtney.shane",
            "c_teamPagesURL": "https://advisor.morganstanley.com/the-marinelli-group",
            "emails": ["Courtney.Shane@morganstanley.com"],
            "address": {"city": "Boston", "region": "MA"},
        })
        self.assertEqual(
            "https://advisor.morganstanley.com/courtney.shane", row["Profile URL"])
        self.assertEqual(
            "https://advisor.morganstanley.com/the-marinelli-group",
            row["Team Page URL"])
        self.assertEqual("Courtney.Shane@morganstanley.com", row["Email"])

    def test_team_page_name_confirms_existing_directory_advisor_without_email(self):
        card = {"name": "Devin Condron", "titles": [], "phone": "",
                "email": "", "personId": "11085242", "linkedin": ""}
        rows, summary = merge_members([advisor(email="")], [page([card])])
        self.assertEqual(1, len(rows))
        self.assertEqual(1, summary["directoryCardsConfirmed"])

    def test_published_email_domain_is_fail_closed(self):
        self.assertEqual("person@ms.com", email("Person@MS.com"))
        self.assertEqual("", email("person@morganstanely.com"))
        self.assertEqual("", email("person@example.com"))

    def test_directory_advisor_wins_and_team_member_is_added_without_crd(self):
        cards = [
            {
                "name": "Devin Condron",
                "titles": ["Managing Director", "Private Wealth Advisor"],
                "phone": "+16177578805",
                "email": "devin.condron@morganstanleypwm.com",
                "personId": "11085242", "linkedin": "",
            },
            {
                "name": "Elijah Brown",
                "titles": ["PWM Analyst", "Financial Planning Specialist"],
                "phone": "+16174786511",
                "email": "elijah.brown@morganstanley.com",
                "personId": "Elijah.Brown@morganstanley.com",
                "linkedin": "",
            },
        ]
        rows, summary = merge_members([advisor()], [page(cards)])
        self.assertEqual(2, len(rows))
        self.assertEqual("ADVISOR", rows[0]["Entity Type"])
        self.assertEqual("Private Wealth Advisor", rows[0]["Primary Title"])
        self.assertEqual("TEAM_MEMBER", rows[1]["Entity Type"])
        self.assertEqual("Elijah Brown", rows[1]["Name"])
        self.assertEqual("PWM Analyst, Financial Planning Specialist",
                         rows[1]["Primary Title"])
        self.assertEqual("Boston", rows[1]["City"])
        self.assertEqual("elijah.brown@morganstanley.com", rows[1]["Email"])
        self.assertNotIn("advisor_crd", rows[1])
        self.assertEqual(1, summary["directoryCardsConfirmed"])
        self.assertEqual(1, summary["teamMembersAdded"])

    def test_duplicate_team_member_email_is_suppressed_across_pages(self):
        member = {
            "name": "Hannah Touchette",
            "titles": ["PWM Registered Client Service Associate"],
            "phone": "+16177578811",
            "email": "hannah.touchette@morganstanleypwm.com",
            "personId": "hannah.touchette@morganstanleypwm.com",
            "linkedin": "",
        }
        rows, summary = merge_members([advisor()], [page([member]), page([member])])
        self.assertEqual(2, len(rows))
        self.assertEqual(1, summary["teamMembersAdded"])
        self.assertEqual(0, summary["directoryCardsConfirmed"])
        self.assertEqual(1, summary["duplicateCardsSuppressed"])

    def test_only_transient_page_outcomes_are_retried(self):
        self.assertFalse(needs_retry({"status": 200}))
        self.assertFalse(needs_retry({"status": 404}))
        for status in (0, 408, 425, 429, 500, 503):
            self.assertTrue(needs_retry({"status": status}))
        self.assertTrue(needs_retry(None))

    def test_failed_page_contributes_no_people(self):
        rows, summary = merge_members([advisor()], [page([{
            "name": "Wrong Person", "titles": [], "phone": "",
            "email": "wrong@morganstanley.com", "personId": "wrong",
        }], status=403)])
        self.assertEqual(1, len(rows))
        self.assertEqual(0, summary["teamMembersAdded"])

    def test_full_throttle_batch_trips_the_circuit_breaker(self):
        self.assertTrue(blocked_batch([{"status": 0}, {"status": 429}]))
        self.assertFalse(blocked_batch([{"status": 404}, {"status": 0}]))
        self.assertFalse(blocked_batch([{"status": 200}, {"status": 429}]))
        self.assertFalse(blocked_batch([]))

    def test_browser_parser_uses_structured_person_attributes(self):
        for marker in ('.BiosCarousel-slide', 'itemprop="name"',
                       'itemprop="jobTitle"', 'data-goto-id', 'href^="tel:"',
                       'AbortController'):
            self.assertIn(marker, EXTRACT_BATCH_JS)

    def test_crawler_uses_an_isolated_context_and_never_user_tabs(self):
        source = (ROOT / "src" / "morgan_team_pages.py").read_text(
            encoding="utf-8")
        self.assertIn("context = browser.new_context()", source)
        self.assertNotIn("context = browser.contexts[0]", source)
        self.assertNotRegex(source, r"(?m)^\s*browser\.close\(\)")

    def test_bootstrap_retries_with_a_fresh_page(self):
        class FakePage:
            def __init__(self, fails):
                self.fails = fails
                self.closed = False

            def goto(self, *_args, **_kwargs):
                if self.fails:
                    raise TimeoutError("temporary")

            def close(self):
                self.closed = True

        class FakeContext:
            def __init__(self):
                self.pages = [FakePage(True), FakePage(False)]

            def new_page(self):
                return self.pages.pop(0)

        context = FakeContext()
        page = open_bootstrap(
            context, "https://advisor.morganstanley.com/team",
            attempts=2, cooldown_seconds=0)
        self.assertFalse(page.closed)

    def test_refresh_comparison_reports_added_improved_and_removed(self):
        old = [
            advisor(email=""),
            {**advisor("Retired Person", "retired@morganstanley.com"),
             "FA Number": "old-2"},
        ]
        old[0]["FA Number"] = "fa-1"
        new = [
            {**advisor(), "FA Number": "fa-1"},
            {**advisor("New Person", "new@morganstanley.com"),
             "FA Number": "new-3"},
        ]
        details, summary = compare_rosters(old, new)
        self.assertEqual(1, summary["improved"])
        self.assertEqual(1, summary["added"])
        self.assertEqual(1, summary["removed"])
        improved = next(row for row in details if row["status"] == "improved")
        self.assertIn("Email", improved["improved_fields"])

    def test_branch_email_audit_confirms_hidden_team_card_email(self):
        index = [{
            "Team Name": "The Marinelli Group",
            "Team Page URL":
                "https://advisor.morganstanley.com/the-marinelli-group",
            "Published Team Emails":
                "courtney.shane@morganstanley.com | missing@morganstanley.com",
        }]
        parsed = page([{
            "name": "Courtney Shane", "titles": [], "phone": "",
            "email": "Courtney.Shane@morganstanley.com",
            "personId": "Courtney.Shane@morganstanley.com", "linkedin": "",
        }])
        parsed["url"] = parsed["finalUrl"] = (
            "https://advisor.morganstanley.com/the-marinelli-group")
        pages = [parsed]
        rows, summary = audit_team_emails(index, pages)
        self.assertEqual(1, summary["confirmedEmails"])
        self.assertEqual(1, summary["branchOnlyEmails"])
        self.assertEqual(
            "confirmed",
            next(row for row in rows
                 if row["email"] == "courtney.shane@morganstanley.com")["status"])

    def test_branch_email_audit_uses_directory_email_for_numeric_card_id(self):
        index = [{
            "Team Name": "The Condron Team",
            "Team Page URL": "https://advisor.morganstanley.com/the-condron-team",
            "Published Team Emails": "devin.condron@morganstanleypwm.com",
        }]
        rows, summary = audit_team_emails(index, [page([{
            "name": "Devin Condron", "titles": [], "phone": "",
            "email": "", "personId": "11085242", "linkedin": "",
        }])], [advisor()])
        self.assertEqual(1, summary["confirmedEmails"])
        self.assertEqual(0, summary["branchOnlyEmails"])

    def test_coverage_title_keys_are_case_insensitively_unique(self):
        import tempfile
        from morgan_team_pages import write_coverage_report
        rows = [
            {"Entity Type": "TEAM_MEMBER", "Primary Title": "Team Administrator"},
            {"Entity Type": "TEAM_MEMBER", "Primary Title": "Team administrator"},
        ]
        with tempfile.TemporaryDirectory() as folder:
            target = pathlib.Path(folder) / "coverage.json"
            write_coverage_report(target, rows, {})
            titles = json.loads(target.read_text())["addedByTitle"]
        self.assertEqual(1, len(titles))
        self.assertEqual(2, next(iter(titles.values())))


if __name__ == "__main__":
    unittest.main()
