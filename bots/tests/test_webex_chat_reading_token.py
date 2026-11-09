from unittest.mock import MagicMock, patch

from django.test import TestCase

from bots.webex_bot_adapter.webex_chat_reading_token import create_chat_reading_guest_token
from bots.webex_bot_adapter.webex_guest_token import WebexAuthError

MEETING_URL = "https://acme.my.webex.com/meet/jane.doe"
MEETING_PAGE = 'var x = {"csrfToken":"the-csrf","guestCookie":"the-guest-cookie"};window.location = "webexapp://meet?jt=abc&sip=12345@acme.my.webex.com&joinTXId=the-join-id&flag=1";'


def _response(ok=True, status=200, json_data=None, text=""):
    response = MagicMock()
    response.ok = ok
    response.status_code = status
    response.text = text
    response.json.return_value = json_data or {}
    return response


class WebexChatReadingTokenTest(TestCase):
    def _session_with(self, page=None, guest_cookie=None, prejoin=None, token=None):
        session = MagicMock()
        session.get.side_effect = [page or _response(text=MEETING_PAGE)]
        session.post.side_effect = [
            guest_cookie or _response(),
            prejoin or _response(json_data={"guestJWT": "the-ticket"}),
            token or _response(json_data={"access_token": "the-guest-token"}),
        ]
        return session

    def test_it_mints_a_token_from_the_web_client_guest_flow(self):
        session = self._session_with()
        with patch("bots.webex_bot_adapter.webex_chat_reading_token.requests.Session", return_value=session):
            token = create_chat_reading_guest_token(MEETING_URL, "chat-client-id", "chat-client-secret", "Bot")

        self.assertEqual(token, "the-guest-token")

        # The guest ticket is exchanged at the guest identity broker with the chat-reading client id and secret.
        exchange_call = session.post.call_args_list[2]
        self.assertEqual(exchange_call.kwargs["auth"], ("chat-client-id", "chat-client-secret"))
        self.assertEqual(exchange_call.kwargs["data"]["webexapp_jwt"], "the-ticket")

        # The per-meeting values come from the meeting page, not from the caller.
        prejoin_call = session.post.call_args_list[1]
        self.assertIn("the-join-id", prejoin_call.kwargs["data"])

    def test_a_meeting_page_missing_the_join_id_is_an_authorization_error(self):
        session = self._session_with(page=_response(text="no join id here"))
        with patch("bots.webex_bot_adapter.webex_chat_reading_token.requests.Session", return_value=session):
            with self.assertRaises(WebexAuthError):
                create_chat_reading_guest_token(MEETING_URL, "id", "secret", "Bot")

    def test_a_failed_token_exchange_is_an_authorization_error(self):
        session = self._session_with(token=_response(ok=False, status=401, text="nope"))
        with patch("bots.webex_bot_adapter.webex_chat_reading_token.requests.Session", return_value=session):
            with self.assertRaises(WebexAuthError):
                create_chat_reading_guest_token(MEETING_URL, "id", "secret", "Bot")

    def test_a_prejoin_without_a_ticket_is_an_authorization_error(self):
        session = self._session_with(prejoin=_response(json_data={}))
        with patch("bots.webex_bot_adapter.webex_chat_reading_token.requests.Session", return_value=session):
            with self.assertRaises(WebexAuthError):
                create_chat_reading_guest_token(MEETING_URL, "id", "secret", "Bot")
