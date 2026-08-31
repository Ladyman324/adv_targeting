import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roster_greetings import resolve_roster_greeting


CASES = [
    ("Angela (Angie) Smith", "angela.smith@firm.example", "Angela", "", "Angie", "Smith", "Angie"),
    ("Michael (Mike) Smith", "michael.smith@firm.example", "Michael", "", "Mike", "Smith", "Mike"),
    ("Anthony (Tony) Smith", "anthony.smith@firm.example", "Anthony", "", "Tony", "Smith", "Tony"),
    ("Elizabeth (Bets) Smith", "elizabeth.smith@firm.example", "Elizabeth", "", "", "Smith", "Bets"),
    ("Phillip (Phil) Smith", "phillip.smith@firm.example", "Phillip", "", "", "Smith", "Phil"),
    ("Brad Smith, CFP LUTCF", "brad.smith@firm.example", "Bradford", "", "Brad", "Smith", "Brad"),
    ("Kim Smith, ChFC(R), CLU", "kim.smith@firm.example", "Kimberly", "", "Kim", "Smith", "Kim"),
    ("A. Alan Smith III", "alan.smith@firm.example", "Albert", "Alan", "", "Smith", "Alan"),
    ("Ande Smith Jr.", "ande.smith@firm.example", "Anderson", "", "", "Smith", "Ande"),
    ("A. CHRISTOPHER ENGLE,CFP LUTCF", "chris.engle@lpl.com", "Albert", "Christopher", "", "ENGLE", "Chris"),
    ("A. Denver Smith", "", "Arthur", "Denver", "", "Smith", "Denver"),
    ("G. Kyle Smith", "", "George", "Kyle", "", "Smith", "Kyle"),
    ("B. Wade Smith", "", "Benjamin", "Wade", "", "Smith", "Wade"),
    ("M. Cameron Smith", "cam.smith@firm.example", "Michael", "Cameron", "", "Smith", "Cam"),
    ("Mr. Augustus Bo Smith", "bo.smith@firm.example", "Augustus", "", "Bo", "Smith", "Bo"),
    ("Mr. Adam Smith", "adam.smith@firm.example", "Adam", "", "", "Smith", "Adam"),
    ("Dr. Gina Smith", "gina.smith@firm.example", "Gina", "", "", "Smith", "Gina"),
    ("Alex Conaway", "alexander.conaway@firm.example", "Alexander", "", "", "Conaway", "Alex"),
    ("Alan McKnight", "albert.mcknight@firm.example", "Albert", "Alan", "", "McKnight", "Alan"),
    ("Bill Ramos", "william.ramos@firm.example", "William", "", "Bill", "Ramos", "Bill"),
]


class RosterGreetingTests(unittest.TestCase):
    def decide(self, roster, email, first, middle="", used="", last="Person",
               unique=True, authoritative=True):
        return resolve_roster_greeting(
            roster_name=roster, email=email, sec_first=first,
            sec_middle=middle, sec_used=used, sec_last=last,
            email_unique=unique, authoritative_domain=authoritative)

    def test_cases_11_through_30(self):
        for roster, email, first, middle, used, last, expected in CASES:
            with self.subTest(roster=roster):
                d = self.decide(roster, email, first, middle, used, last)
                self.assertEqual(expected, d.greeting)
                expected_last = "Engle" if last == "ENGLE" else last
                self.assertEqual(expected_last, d.last_name)
                self.assertRegex(d.evidence_hash, r"^[0-9a-f]{64}$")

    def test_email_inference_is_fail_closed(self):
        variants = [
            ("chris.engle@lpl.com", False, True),
            ("chris.engle@lpl.com", True, False),
            ("info.engle@lpl.com", True, True),
            ("chris.smith@lpl.com", True, True),
            ("chip.engle@lpl.com", True, True),
        ]
        for email, unique, authoritative in variants:
            with self.subTest(email=email):
                d = self.decide("A. Christopher Engle", email, "Albert",
                                "Christopher", last="Engle", unique=unique,
                                authoritative=authoritative)
                self.assertEqual("Christopher", d.greeting)

    def test_parenthetical_middle_name_is_not_a_preference(self):
        d = self.decide("John (David) Smith", "john.smith@example.com",
                        "John", "David", last="Smith")
        self.assertEqual("John", d.greeting)

    def test_suffix_is_not_exported_as_last_name(self):
        d = self.decide("Lynn Trusty Shaw II", "lynn.shawii@raymondjames.com",
                        "Lynn", "Trusty", last="SHAW II")
        self.assertEqual("Lynn", d.greeting)
        self.assertEqual("Shaw", d.last_name)
        self.assertEqual("Shaw II", d.presentation_last_name)

    def test_current_roster_surname_can_use_sec_filed_alias(self):
        d = resolve_roster_greeting(
            roster_name="Nicole Tesoriero", email="nicole.tesoriero@ubs.com",
            sec_first="Nicole", sec_middle="", sec_used="",
            sec_last="Flores", sec_aliases=("Flores", "Tesoriero"),
            email_unique=True, authoritative_domain=True)
        self.assertEqual("Nicole", d.greeting)
        self.assertEqual("Tesoriero", d.last_name)
        self.assertEqual("Tesoriero", d.presentation_last_name)
        self.assertEqual("Nicole Tesoriero", d.presentation_name)

    def test_initial_and_multiword_sec_names_are_readable(self):
        examples = [
            (("T. Naples", "greg.naples@example.com", "T.", "Gregory",
              "T GREGORY", "Naples"), "T. (Greg) Naples"),
            (("M Neuendorf", "mjayne.neuendorf@example.com", "M", "Jayne",
              "M. JAYNE", "Neuendorf"), "M. (Jayne) Neuendorf"),
            (("K Thoeni", "nicholas.thoeni@example.com", "KARL JAMES",
              "NICHOLAS", "", "Thoeni"),
             "Karl James (Nicholas) Thoeni"),
        ]
        for args, expected in examples:
            with self.subTest(expected=expected):
                self.assertEqual(expected, self.decide(*args).presentation_name)

    def test_reviewed_act_greeting_drives_presentation_not_identity(self):
        d = resolve_roster_greeting(
            roster_name="Robert Ladyman", email="bladyman@eicatlanta.com",
            sec_first="Robert", sec_middle="Murphy", sec_used="",
            sec_last="Ladyman", approved_greeting="Bo")
        self.assertEqual("Robert", d.greeting)
        self.assertEqual("Robert (Bo) Ladyman", d.presentation_name)

    def test_surname_contradiction_falls_back_to_sec(self):
        d = self.decide("Wrong Person", "wrong.person@example.com",
                        "Albert", last="Engle")
        self.assertEqual("Albert", d.greeting)
        self.assertEqual("Engle", d.last_name)

    def test_hash_is_deterministic_and_safety_bound(self):
        args = ("A. Christopher Engle", "chris.engle@lpl.com",
                "Albert", "Christopher", "", "Engle")
        one = self.decide(*args)
        two = self.decide(*args)
        changed = self.decide(*args, unique=False)
        self.assertEqual(one.evidence_hash, two.evidence_hash)
        self.assertNotEqual(one.evidence_hash, changed.evidence_hash)


if __name__ == "__main__":
    unittest.main()
