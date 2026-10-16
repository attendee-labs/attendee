import json
import logging
from typing import Callable

from bots.web_bot_adapter import WebBotAdapter
from bots.web_bot_adapter.ui_methods import UiInfinitelyRetryableException
from bots.webex_bot_adapter.webex_guest_token import WebexAuthError
from bots.webex_bot_adapter.webex_ui_methods import WebexUIMethods

logger = logging.getLogger(__name__)

# The hosts the page contacts to load the SDK, to join the meeting and to receive captions and participants. Webex serves all of
# them from its own domains. A domain also allows its subdomains.
WEBEX_DOMAIN_ALLOWLIST = ["webex.com", "ciscospark.com", "wbx2.com", "unpkg.com", "127.0.0.1"]


class UiWebexAuthorizationFailedException(UiInfinitelyRetryableException):
    pass


class WebexBotAdapter(WebBotAdapter, WebexUIMethods):
    def __init__(self, *args, create_guest_token_callback: Callable[[], str], **kwargs):
        super().__init__(*args, **kwargs)
        # Doing it via callback so we don't keep the Service App credentials in memory
        self.create_guest_token_callback = create_guest_token_callback
        self.failed_to_join_reason = None
        self.sent_authorization_failed_message = False

    def get_chromedriver_payload_file_names(self):
        return ["webex_bot_adapter/webex_chromedriver_payload.js"]

    def get_websocket_port(self):
        return 8099

    def get_staged_bot_join_delay_seconds(self):
        return 5

    def subclass_specific_initial_data_code(self):
        return f"""
            window.webexInitialData = {{
                guestToken: {json.dumps(self.create_guest_token())},
                meetingUrl: {json.dumps(self.meeting_url)},
                botName: {json.dumps(self.display_name)},
            }}
        """

    def create_guest_token(self):
        try:
            guest_token = self.create_guest_token_callback()
            logger.info("Created a Webex guest token")
            return guest_token
        except WebexAuthError as e:
            logger.error(f"Could not create a Webex guest token: {e}")
            # The controller cleans the bot up once it has the message, which ends the retries of this exception
            if not self.sent_authorization_failed_message:
                self.sent_authorization_failed_message = True
                self.send_message_callback({"message": self.Messages.COULD_NOT_CONNECT_TO_MEETING})
            raise UiWebexAuthorizationFailedException("Could not create a Webex guest token") from e

    def subclass_specific_domain_allowlist(self):
        return list(WEBEX_DOMAIN_ALLOWLIST)

    def subclass_specific_after_bot_joined_meeting(self):
        self.after_bot_can_record_meeting()

    def subclass_specific_handle_failed_to_join(self, reason):
        # attempt_to_join_meeting turns this into the exception that reports it to the controller
        self.failed_to_join_reason = reason
