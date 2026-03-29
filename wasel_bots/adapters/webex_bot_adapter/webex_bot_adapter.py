import json
import logging
import os

from wasel_bots.utils.meeting_url_utils import parse_webex_join_url
from bots.web_bot_adapter import WebBotAdapter
from .webex_ui_methods import WebexUIMethodsException, WebexUIMethods
from .webex_static_server import start_webex_static_server

logger = logging.getLogger(__name__)


class WebexBotAdapter(WebBotAdapter, WebexUIMethods):
    """
    Webex Bot Adapter for joining and recording Webex meetings using the Webex Web SDK.

    This adapter integrates with the WebBotAdapter framework to provide:
    - Multistream audio recording (up to 3 simultaneous speaker streams)
    - Participant tracking and correlation
    - Transcription support
    - Video display (optional)
    - Active speaker detection with enhanced correlation algorithms
    """

    def __init__(
        self,
        *args,
        webex_bot_connection,  # WebexBotConnection model instance
        webex_meeting_url: str,
        webex_meeting_password: str | None = None,
        enable_transcription: bool = True,
        **kwargs,
    ):
        """
        Initialize the Webex Bot Adapter.

        Parameters
        ----------
        webex_bot_connection : WebexBotConnection
            WebexBotConnection model instance with access token credentials
        webex_meeting_url : str
            The Webex meeting URL or meeting number
        webex_meeting_password : str | None
            Optional meeting password
        enable_transcription : bool
            Whether to enable transcription if available (default True)
        """
        super().__init__(*args, **kwargs)

        self.webex_bot_connection = webex_bot_connection
        self.webex_meeting_url = webex_meeting_url
        self.webex_meeting_password = webex_meeting_password
        self.enable_transcription = enable_transcription

        # Parse meeting URL if needed
        self.meeting_destination = webex_meeting_url

        # Track authentication state
        self.authentication_attempts = 0
        self.max_authentication_attempts = 3
        
        # Console log tracking - DISABLED to reduce verbose logging
        self.console_logs = []
        self.enable_console_logging = False

        # Start static HTTP server to serve the Webex SDK page
        self.static_server_port = start_webex_static_server()
        logger.info(f"Started Webex static server on port {self.static_server_port}")

    def get_static_page_url(self):
        """Return the URL to the locally served Webex page"""
        return f"http://127.0.0.1:{self.static_server_port}/index.html"

    def get_chromedriver_payload_file_name(self):
        """Return the path to the Webex-specific JavaScript payload."""
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "webex_autofill_payload.js")

    def get_websocket_port(self):
        """Return the WebSocket port for browser-Python communication."""
        return 8766  # Different from Zoom (8765) to avoid conflicts

    def get_webex_access_token(self):
        """
        Get a valid Webex access token for the bot.

        Returns
        -------
        str
            Valid Webex access token
        """
        from .webex_token_utils import get_webex_access_token_for_bot

        # Get access token, refreshing if needed
        return get_webex_access_token_for_bot(self.webex_bot_connection)

    def subclass_specific_initial_data_code(self):
        """
        Provide Webex-specific initialization data to the browser context.

        This JavaScript code will be executed in the browser before loading
        the main payload, making the configuration available to the Webex SDK.
        """
        # Get fresh access token
        access_token = self.get_webex_access_token()
        # Use self.websocket_port (set by base class after server starts) 
        # with fallback to get_websocket_port()
        websocket_port = self.websocket_port or self.get_websocket_port()
        websocket_url = f"ws://localhost:{websocket_port}"

        return f"""
            window.webexInitialData = {{
                accessToken: {json.dumps(access_token)},
                meetingDestination: {json.dumps(self.meeting_destination)},
                meetingPassword: {json.dumps(self.webex_meeting_password or "")},
                websocketUrl: {json.dumps(websocket_url)},
                websocketPort: {json.dumps(websocket_port)},
                enableTranscription: {json.dumps(self.enable_transcription)},
                botName: {json.dumps(self.display_name)},
                disableIncomingVideo: {json.dumps(self.disable_incoming_video)},
            }};
            console.log('[Python Adapter] webexInitialData set');
        """

    def subclass_specific_after_bot_joined_meeting(self):
        """
        Called after the bot has successfully joined the meeting.

        For Webex, we can immediately start recording as there's typically
        no separate recording permission request (unlike Zoom).
        """
        logger.info("Webex bot joined meeting, starting recording")
        self.after_bot_can_record_meeting()

    def subclass_specific_handle_failed_to_join(self, reason):
        """
        Handle Webex-specific join failures.

        Parameters
        ----------
        reason : dict
            Dictionary containing error information from the Webex SDK
        """
        error_code = reason.get("errorCode")
        error_message = reason.get("errorMessage", "Unknown error")

        logger.warning(f"Webex join failed: {error_code} - {error_message}")

        # Handle authentication failures
        if error_code in [401, 403]:  # Unauthorized or Forbidden
            self.authentication_attempts += 1

            if self.authentication_attempts >= self.max_authentication_attempts:
                self.send_message_callback({
                    "message": "Webex authentication failed after multiple attempts",
                    "error_code": error_code,
                    "error_message": error_message,
                })
            else:
                logger.info(f"Retrying authentication (attempt {self.authentication_attempts}/{self.max_authentication_attempts})")
                # Could raise a retryable exception here if needed

        # Handle meeting not found
        elif error_code == 404:
            self.send_meeting_not_found_message()

        # Handle invalid password
        elif error_code == 423:  # Locked (password protected)
            self.send_incorrect_password_message()

        # Generic failure
        else:
            self.send_message_callback({
                "message": "Webex meeting join failed",
                "error_code": error_code,
                "error_message": error_message,
            })

    def get_staged_bot_join_delay_seconds(self):
        """
        Return the delay before joining the meeting (for staged bots).

        Webex typically doesn't need as long a delay as Zoom.
        """
        return 3

    def is_sent_video_still_playing(self):
        """
        Check if a sent video is still playing.

        Note: Video sending is not implemented in initial Webex adapter.
        """
        # TODO: Implement if video sending is needed
        return False

    def send_video(self, video_url):
        """
        Send a video to the meeting.

        Note: Video sending is not implemented in initial Webex adapter.
        """
        # TODO: Implement if video sending is needed
        logger.warning("Video sending not yet implemented for Webex adapter")

    def send_chat_message(self, text, to_user_uuid=None):
        """
        Send a chat message to the meeting.

        Parameters
        ----------
        text : str
            The message text to send
        to_user_uuid : str | None
            Optional: send to specific user (None = send to all)
        """
        if not self.ready_to_send_chat_messages:
            logger.warning("Cannot send chat message - not ready yet")
            return

        self.driver.execute_script(
            "window?.sendChatMessage(arguments[0], arguments[1]);",
            text,
            to_user_uuid
        )

    def change_gallery_view_page(self, next_page: bool):
        """
        Change the gallery view page (next/previous).

        Parameters
        ----------
        next_page : bool
            True for next page, False for previous page
        """
        # TODO: Implement if gallery view pagination is needed
        logger.info(f"Gallery view page change requested: {'next' if next_page else 'previous'}")

    def subclass_specific_before_driver_close(self):
        """
        Called before the browser driver is closed during cleanup.
        
        For Webex, we call leaveMeeting() in JavaScript to properly
        disconnect from the Webex meeting before closing the browser.
        
        Since leaveMeeting() is async, we use execute_async_script with
        a callback to wait for it to complete, with a fallback to
        synchronous execution if the async approach fails.
        """
        import time
        try:
            logger.info("Webex: calling leaveMeeting() before driver close")
            # First check if there's still a meeting to leave
            has_meeting = self.driver.execute_script("return !!window.meeting;")
            if not has_meeting:
                logger.info("Webex: no meeting object found, skipping leaveMeeting")
                return

            # Use execute_async_script to properly await the async leaveMeeting()
            # Set a generous timeout for the async script
            self.driver.set_script_timeout(10)
            try:
                self.driver.execute_async_script("""
                    var callback = arguments[arguments.length - 1];
                    if (window.leaveMeeting) {
                        window.leaveMeeting()
                            .then(function() { callback('left'); })
                            .catch(function(err) { callback('error: ' + err.message); });
                    } else if (window.meeting) {
                        window.meeting.leave()
                            .then(function() { callback('left'); })
                            .catch(function(err) { callback('error: ' + err.message); });
                    } else {
                        callback('no_meeting');
                    }
                """)
                logger.info("Webex: leaveMeeting completed via async script")
            except Exception as async_err:
                logger.warning(f"Webex: async leaveMeeting failed ({async_err}), trying sync fallback")
                # Fallback: fire-and-forget with sleep
                self.driver.execute_script("""
                    if (window.leaveMeeting) {
                        window.leaveMeeting();
                    } else if (window.meeting) {
                        window.meeting.leave()
                            .then(() => console.log('[Webex] Left meeting'))
                            .catch(err => console.error('[Webex] Error leaving:', err));
                    }
                """)
                time.sleep(3)
        except Exception as e:
            logger.warning(f"Error during Webex pre-close cleanup: {e}")

    def add_subclass_specific_chrome_options(self, options):
        """
        Add Webex-specific Chrome options.
        
        Parameters
        ----------
        options : ChromeOptions
            Chrome options to modify
        """
        # Enable browser console logging
        if self.enable_console_logging:
            options.set_capability('goog:loggingPrefs', {
                'browser': 'ALL',
                'performance': 'ALL'
            })
            logger.info("Enabled browser console logging for debugging")
    
    def get_browser_console_logs(self):
        """
        Retrieve all browser console logs.
        
        Returns
        -------
        list
            List of console log entries
        """
        if not self.driver:
            return []
        
        try:
            logs = self.driver.get_log('browser')
            
            # Store and format logs
            for log_entry in logs:
                formatted_log = {
                    'timestamp': log_entry.get('timestamp'),
                    'level': log_entry.get('level'),
                    'message': log_entry.get('message'),
                    'source': log_entry.get('source', 'browser')
                }
                self.console_logs.append(formatted_log)
                
                # Also log to Python logger
                log_level = log_entry.get('level', 'INFO')
                log_message = log_entry.get('message', '')
                
                if log_level == 'SEVERE':
                    logger.error(f"[BROWSER] {log_message}")
                elif log_level == 'WARNING':
                    logger.warning(f"[BROWSER] {log_message}")
                else:
                    logger.info(f"[BROWSER] {log_message}")
            
            return logs
        except Exception as e:
            logger.warning(f"Error retrieving browser console logs: {e}")
            return []
    
    def print_browser_console_logs(self):
        """
        Print all collected browser console logs to the Python logger.
        """
        logger.info("=" * 80)
        logger.info("BROWSER CONSOLE LOGS")
        logger.info("=" * 80)
        
        logs = self.get_browser_console_logs()
        
        if not logs:
            logger.info("No console logs available")
        else:
            for log in logs:
                level = log.get('level', 'INFO')
                message = log.get('message', '')
                logger.info(f"[{level}] {message}")
        
        logger.info("=" * 80)

