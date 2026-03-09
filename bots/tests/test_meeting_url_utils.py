# test_meeting_utils.py
import base64
import json
import unittest

from bots.meeting_url_utils import MeetingTypes, domain_and_subdomain_from_url, meeting_type_from_url, normalize_meeting_url, parse_zoom_join_url, root_domain_from_url


class TestMeetingUrlUtils(unittest.TestCase):
    def test_root_domain_from_url(self):
        self.assertEqual(root_domain_from_url("https://meet.google.com/abc-defg-hij"), "google.com")
        self.assertEqual(root_domain_from_url("https://teams.microsoft.com/l/meetup-join/..."), "microsoft.com")
        self.assertEqual(root_domain_from_url("https://zoom.us/j/123456789"), "zoom.us")
        self.assertIsNone(root_domain_from_url(""))
        self.assertIsNone(root_domain_from_url(None))

    def test_domain_and_subdomain_from_url(self):
        self.assertEqual(domain_and_subdomain_from_url("https://meet.google.com/abc-defg-hij"), "meet.google.com")
        self.assertEqual(domain_and_subdomain_from_url("https://teams.microsoft.com/l/meetup-join/..."), "teams.microsoft.com")
        self.assertEqual(domain_and_subdomain_from_url("https://zoom.us/j/123456789"), ".zoom.us")
        self.assertIsNone(domain_and_subdomain_from_url(""))
        self.assertIsNone(domain_and_subdomain_from_url(None))

    def test_teams_live_urls(self):
        self.assertEqual(meeting_type_from_url("https://teams.live.com/meet/9876543210?p=abc"), MeetingTypes.TEAMS)
        self.assertEqual(meeting_type_from_url("https://teams.microsoft.com/meet/9876543210?p=qHDqtvFSfIg1rT"), MeetingTypes.TEAMS)
        self.assertEqual(meeting_type_from_url("https://teams.live.com/meet/9876543210?p=abc>"), MeetingTypes.TEAMS)
        self.assertEqual(normalize_meeting_url("https://teams.live.com/meet/9876543210?p=abc>")[1], "https://teams.live.com/meet/9876543210?p=abc")

    def test_teams_meetup_join_urls(self):
        teams_url_with_trailing_carat = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_NjnnnnnnnnnnnnnnnnnnnnnnnnnnMzctODQwYTBmMDQ4MzQ2%40thread.v2/0?context=%7b%22Tid%22%3a%22b8291b4b-aaaa-bbbb-8a00-9d5fc37b9a77%22%2c%22Oid%22%3a%22216d2e11-45cc-9999-9689-d05554e5c1d1%22%7d>"
        self.assertEqual(meeting_type_from_url(teams_url_with_trailing_carat), MeetingTypes.TEAMS)
        self.assertEqual(normalize_meeting_url(teams_url_with_trailing_carat)[1], 'https://teams.microsoft.com/l/meetup-join/19:meeting_NjnnnnnnnnnnnnnnnnnnnnnnnnnnMzctODQwYTBmMDQ4MzQ2@thread.v2/0?context={"Tid":"b8291b4b-aaaa-bbbb-8a00-9d5fc37b9a77","Oid":"216d2e11-45cc-9999-9689-d05554e5c1d1"}')

    def test_normalize_meeting_url(self):
        self.assertEqual(normalize_meeting_url("https://zoom.us/j/123456789")[1], "https://zoom.us/j/123456789")
        self.assertEqual(normalize_meeting_url("zoom.us/j/123456789")[1], "https://zoom.us/j/123456789")
        self.assertEqual(normalize_meeting_url("https://meet.google.com/abc-defg-hij")[1], "https://meet.google.com/abc-defg-hij")

        self.assertEqual(normalize_meeting_url("meet.google.com/abc-defg-hij")[1], "https://meet.google.com/abc-defg-hij")
        self.assertEqual(normalize_meeting_url("https://teams.microsoft.com/l/meetup-join/19%3ameeting_NjnnnnnnnnnnnnnnnnnnnnnnnnnnMzctODQwYTBmMDQ4MzQ2%40thread.v2/0?context=%7b%22Tid%22%3a%22b8291b4b-aaaa-bbbb-8a00-9d5fc37b9a77%22%2c%22Oid%22%3a%22216d2e11-45cc-9999-9689-d05554e5c1d1%22%7d>")[1], 'https://teams.microsoft.com/l/meetup-join/19:meeting_NjnnnnnnnnnnnnnnnnnnnnnnnnnnMzctODQwYTBmMDQ4MzQ2@thread.v2/0?context={"Tid":"b8291b4b-aaaa-bbbb-8a00-9d5fc37b9a77","Oid":"216d2e11-45cc-9999-9689-d05554e5c1d1"}')
        self.assertEqual(normalize_meeting_url("https://teams.microsoft.com/meet/999999999999?p=aaaaalRE2XBPsAr00W")[1], "https://teams.microsoft.com/meet/999999999999?p=aaaaalRE2XBPsAr00W")

        self.assertEqual(normalize_meeting_url("https://teams.live.com/meet/999999999999?p=aaaaalRE2XBPsAr00W")[1], "https://teams.live.com/meet/999999999999?p=aaaaalRE2XBPsAr00W")
        self.assertEqual(normalize_meeting_url("https://teams.microsoft.com/dl/launcher/launcher.html?url=/_#/l/meetup-join/19:meeting_NDQ3Y2Q1NDEtY2I5Ni00MzEyLTgzfffffffffffffffffffffffffff@thread.v2/0?context=%7b%22Tid%22%3a%22b00367e2-aaaa-bbbb-cccc-7245d45c0947%22%2c%22Oid%22%3a%2266666666-3a9d-441a-892d-55555555555%22%7d&anon=true&type=meetup-join&deeplinkId=5044eb28-9d33-4309-bf23-3aa618a9e6b0&directDl=true&msLaunch=true&enableMobilePage=true&suppressPrompt=true")[1], 'https://teams.microsoft.com/l/meetup-join/19:meeting_NDQ3Y2Q1NDEtY2I5Ni00MzEyLTgzfffffffffffffffffffffffffff@thread.v2/0?context={"Tid":"b00367e2-aaaa-bbbb-cccc-7245d45c0947","Oid":"66666666-3a9d-441a-892d-55555555555"}')
        coord_json = {"conversationId": "19:meeting_ffffffffffffffffffffffffffffffffffffffffffffffff@thread.v2", "tenantId": "ffffffff-ffff-fff-ffff-6866ff300052", "organizerId": "ffffffff-ffff-4085-acbd-ffffffffffff", "messageId": "0"}
        fake_coord = base64.b64encode(json.dumps(coord_json).encode("utf-8"))
        # Drop trailing equals
        fake_coord = fake_coord.decode("utf-8").rstrip("=")
        self.assertEqual(normalize_meeting_url(f"https://teams.microsoft.com/light-meetings/launch?agent=web&version=25072001100&coords={fake_coord}%3D&deeplinkId=0ac0a241-11111-111-8ddc-4a4b333dd286&correlationId=1111111-1111-1111-1111-11111111111")[1], "https://teams.microsoft.com/l/meetup-join/" + coord_json["conversationId"] + f"/0?context={json.dumps({'Tid': coord_json['tenantId'], 'Oid': coord_json['organizerId']}, separators=(',', ':'))}")

        self.assertEqual(normalize_meeting_url("https://zoom.us/w/123456789?pwd=AbC9xYpQ2LmN7RkT5sVuH4ZbJe1DfG.1&tk=ZkPqN8f2LrS4XyT6wVaE9mHuCdJg5QbA1sDoRtUvWxY.DQkAAAAATESTTOKEN1234567890AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")[1], "https://zoom.us/w/123456789?pwd=AbC9xYpQ2LmN7RkT5sVuH4ZbJe1DfG.1&tk=ZkPqN8f2LrS4XyT6wVaE9mHuCdJg5QbA1sDoRtUvWxY.DQkAAAAATESTTOKEN1234567890AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

    def test_meeting_type_from_url(self):
        self.assertEqual(meeting_type_from_url("https://zoom.us/j/123456789"), MeetingTypes.ZOOM)
        self.assertEqual(meeting_type_from_url("https://zoom.us/j/test"), None)
        self.assertEqual(meeting_type_from_url("https://meet.google.com/abc-defg-hij"), MeetingTypes.GOOGLE_MEET)
        self.assertEqual(meeting_type_from_url("https://teams.microsoft.com/l/meetup-join/19%3ameeting_ABCDEFGHIJKLMNOPQRSTUVWXYZ@thread.v2/0"), None)
        self.assertEqual(meeting_type_from_url("https://teams.live.com/l/meetup-join/19%3ameeting_1234567890ABCDEFGHIJK@thread.v2/0?context=xyz"), None)
        self.assertEqual(meeting_type_from_url("https://teams.live.com/meet/1234567890?p=fffffffffff"), MeetingTypes.TEAMS)
        self.assertEqual(meeting_type_from_url("https://teams.live.com/meet/9876543210?p=abc"), MeetingTypes.TEAMS)
        self.assertIsNone(meeting_type_from_url("https://example.com"))
        self.assertIsNone(meeting_type_from_url(""))
        self.assertIsNone(meeting_type_from_url(None))
        self.assertEqual(meeting_type_from_url("https://teams.microsoft.com/l/meetup-join/19%3ameeting_OTA0nTDmYgItYTlTti00MmRkLTgxODItZGFmNWVmNTJmOGQ4%40thread.v2"), None)

    def test_parse_zoom_webinar_url(self):
        meeting_id, password, registrant_token = parse_zoom_join_url("https://zoom.us/w/111222333?pwd=AbC9xYpQ2LmN7RkT5sVuH4ZbJe1DfG.1&tk=ZkPqN8f2LrS4XyT6wVaE9mHuCdJg5QbA1sDoRtUvWxY.DQkAAAAATESTTOKEN1234567890AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        self.assertEqual(meeting_id, "111222333")
        self.assertEqual(password, "AbC9xYpQ2LmN7RkT5sVuH4ZbJe1DfG.1")
        self.assertEqual(registrant_token, "ZkPqN8f2LrS4XyT6wVaE9mHuCdJg5QbA1sDoRtUvWxY.DQkAAAAATESTTOKEN1234567890AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")


if __name__ == "__main__":
    unittest.main()
