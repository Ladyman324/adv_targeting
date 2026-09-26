import unittest

from src.build_act_mail_codes import deleted_preferences


class DeletedActPreferencesTest(unittest.TestCase):
    def test_only_deleted_explicit_preferences_survive(self):
        records = [
            {"id": "deleted-u", "emailAddress": "UPPER@Example.com",
             "customFields": {"email__y_n": "U"}},
            {"id": "deleted-nc", "altEmailAddress": "nc@example.com",
             "customFields": {"email__y_n": "NC"}},
            {"id": "still-here", "emailAddress": "active@example.com",
             "customFields": {"email__y_n": "U"}},
            {"id": "deleted-bounce", "emailAddress": "bounce@example.com",
             "customFields": {"email__y_n": "N"}},
            {"id": "deleted-prospect", "emailAddress": "prospect@example.com",
             "customFields": {"email__y_n": "P"}},
        ]
        self.assertEqual(
            deleted_preferences(records, {"still-here"}),
            {"upper@example.com": "U", "nc@example.com": "NC"},
        )


if __name__ == "__main__":
    unittest.main()
