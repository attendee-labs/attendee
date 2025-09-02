# test_meeting_utils.py
import unittest

from bots.utils import (
    root_domain_from_url,
    domain_and_subdomain_from_url,
    is_valid_teams_url,
    meeting_type_from_url,
    MeetingTypes
)


class TestMeetingUtils(unittest.TestCase):

    def test_root_domain_from_url(self):
        self.assertEqual(root_domain_from_url("https://meet.google.com/abc-defg-hij"), "google.com")
        self.assertEqual(root_domain_from_url("https://teams.microsoft.com/l/meetup-join/..."), "microsoft.com")
        self.assertEqual(root_domain_from_url("https://zoom.us/j/123456789"), "zoom.us")
        self.assertIsNone(root_domain_from_url(""))
        self.assertIsNone(root_domain_from_url(None))

    def test_domain_and_subdomain_from_url(self):
        self.assertEqual(domain_and_subdomain_from_url("https://meet.google.com/abc-defg-hij"), "meet.google.com")
        self.assertEqual(domain_and_subdomain_from_url("https://teams.microsoft.com/l/meetup-join/..."), "teams.microsoft.com")
        self.assertEqual(domain_and_subdomain_from_url("https://zoom.us/j/123456789"), "zoom.us")
        self.assertIsNone(domain_and_subdomain_from_url(""))
        self.assertIsNone(domain_and_subdomain_from_url(None))

    def test_is_valid_teams_url(self):
        self.assertTrue(is_valid_teams_url("https://teams.microsoft.com/l/meetup-join/19%3ameeting_ABCDEFGHIJKLMNOPQRSTUVWXYZ@thread.v2/0"))
        self.assertTrue(is_valid_teams_url("https://teams.live.com/l/meetup-join/19%3ameeting_1234567890ABCDEFGHIJK@thread.v2/0?context=xyz"))
        self.assertTrue(is_valid_teams_url("https://teams.microsoft.com/l/meetup-join/19%3ameeting_aBcDeFgHiJ1234567890@thread.v2/0?param=value"))
        self.assertTrue(is_valid_teams_url("https://teams.live.com/meet/1234567890"))
        self.assertTrue(is_valid_teams_url("https://teams.live.com/meet/9876543210?context=abc"))
        self.assertTrue(is_valid_teams_url("https://teams.live.com/meet/1122334455?param=value"))

        self.assertFalse(is_valid_teams_url("https://teams.microsoft.com/l/invalid-join/123456"))
        self.assertFalse(is_valid_teams_url("https://teams.live.com/meeting/1234567890"))
        self.assertFalse(is_valid_teams_url("https://zoom.us/j/123456789"))
        self.assertFalse(is_valid_teams_url("https://example.com/teams/meet/12345"))
        self.assertFalse(is_valid_teams_url(""))
        self.assertFalse(is_valid_teams_url(None))

    def test_meeting_type_from_url(self):
        self.assertEqual(meeting_type_from_url("https://zoom.us/j/123456789"), MeetingTypes.ZOOM)
        self.assertEqual(meeting_type_from_url("https://meet.google.com/abc-defg-hij"), MeetingTypes.GOOGLE_MEET)
        self.assertEqual(meeting_type_from_url("https://teams.microsoft.com/l/meetup-join/19%3ameeting_ABCDEFGHIJKLMNOPQRSTUVWXYZ@thread.v2/0"), MeetingTypes.TEAMS)
        self.assertEqual(meeting_type_from_url("https://teams.live.com/l/meetup-join/19%3ameeting_1234567890ABCDEFGHIJK@thread.v2/0?context=xyz"), MeetingTypes.TEAMS)
        self.assertEqual(meeting_type_from_url("https://teams.live.com/meet/1234567890"), MeetingTypes.TEAMS)
        self.assertEqual(meeting_type_from_url("https://teams.live.com/meet/9876543210?context=abc"), MeetingTypes.TEAMS)
        self.assertIsNone(meeting_type_from_url("https://example.com"))
        self.assertIsNone(meeting_type_from_url(""))
        self.assertIsNone(meeting_type_from_url(None))


if __name__ == "__main__":
    unittest.main()
