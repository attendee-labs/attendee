"""
UI Methods for Webex Bot Adapter

Handles browser automation and UI interaction for Webex meetings.
"""
import logging
import time

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait

from bots.web_bot_adapter.ui_methods import (
    UiRetryableException,
    UiInfinitelyRetryableException,
    UiMeetingNotFoundException,
    UiLoginRequiredException,
    UiIncorrectPasswordException,
    UiBlockedByCaptchaException,
    UiCouldNotJoinMeetingWaitingRoomTimeoutException,
    UiRequestToJoinDeniedException,
)

logger = logging.getLogger(__name__)


class WebexUIMethodsException(Exception):
    """Base exception for Webex UI methods"""
    pass


class UiWebexAuthFailedException(UiRetryableException):
    """Raised when Webex authentication fails"""
    pass


class UiWebexMeetingNotFoundException(UiMeetingNotFoundException):
    """Raised when Webex meeting is not found"""
    pass


class UiWebexInvalidPasswordException(UiIncorrectPasswordException):
    """Raised when Webex meeting password is incorrect"""
    pass


class UiWebexSDKInitFailedException(UiRetryableException):
    """Raised when Webex SDK fails to initialize"""
    pass


class UiWebexJoinFailedException(UiInfinitelyRetryableException):
    """Raised when joining Webex meeting fails - infinitely retryable"""
    pass


class UiWebexWaitingRoomTimeoutException(UiCouldNotJoinMeetingWaitingRoomTimeoutException):
    """Raised when bot is stuck in waiting room too long"""
    pass


class WebexUIMethods:
    """
    UI automation methods for Webex meetings.

    Since Webex uses the Web SDK, most interactions are handled through JavaScript
    rather than Selenium DOM manipulation.
    """

    def attempt_to_join_meeting(self):
        """
        Attempt to join the Webex meeting.
        
        This method:
        1. Navigates to the locally served Webex SDK page
        2. Grants necessary permissions
        3. Waits for the page to auto-join the meeting
        4. Verifies successful join
        """
        logger.info("Attempting to join Webex meeting via SDK")

        # Get the static page URL (should be implemented by subclass)
        static_page_url = self.get_static_page_url()
        logger.info(f"Navigating to Webex SDK page: {static_page_url}")

        # Navigate to the page
        self.driver.get(static_page_url)

        # Grant permissions for the local HTTP server origin
        # Extract the port from the URL
        import re
        match = re.search(r':(\d+)', static_page_url)
        port = match.group(1) if match else "8080"
        
        logger.info(f"Granting permissions for origin: http://127.0.0.1:{port}")
        self.driver.execute_cdp_cmd(
            "Browser.grantPermissions",
            {
                "origin": f"http://127.0.0.1:{port}",
                "permissions": ["geolocation", "audioCapture", "displayCapture", "videoCapture"],
            },
        )

        # Wait a moment for page to load
        time.sleep(2)
        
        # Dump page state for debugging
        logger.info("Page loaded, checking initial state...")
        self.dump_page_state()

        # Wait for the page to load and auto-join
        logger.info("Waiting for Webex SDK to initialize and join meeting...")
        self.wait_for_webex_join()

        logger.info("Successfully joined Webex meeting")

    def wait_for_webex_join(self):
        """
        Wait for Webex SDK to initialize and join the meeting.
        
        The JavaScript code (app.js) will:
        1. Initialize the Webex SDK
        2. Connect WebSocket to Python adapter
        3. Join the meeting
        4. May enter lobby/waiting room
        5. Wait for admission
        6. Update window.webexJoinStatus with the result
        7. Send MeetingStatusChange via WebSocket
        
        Raises appropriate exceptions based on the join result.
        """
        max_wait_time = 180  # seconds - 3 minutes total (includes SDK init + lobby wait)
        check_interval = 1  # seconds - check every second
        start_time = time.time()
        last_log_check = time.time()
        log_check_interval = 10  # Check logs every 10 seconds
        
        # Track waiting room time separately
        waiting_room_start = None
        max_waiting_room_time = 120  # 120 seconds to wait in lobby
        last_lobby_log_time = 0

        logger.info("Monitoring Webex join status...")

        while time.time() - start_time < max_wait_time:
            try:
                # Periodically retrieve console logs for debugging (DISABLED - reduces verbose logging)
                # if time.time() - last_log_check >= log_check_interval:
                #     if hasattr(self, 'get_browser_console_logs'):
                #         self.get_browser_console_logs()
                #     last_log_check = time.time()
                
                # Check join status from JavaScript
                join_status = self.driver.execute_script("return window.webexJoinStatus;")
                
                if join_status:
                    status = join_status.get("status")
                    message = join_status.get("message", "")
                    
                    if status in ("joined", "IN_MEETING"):
                        logger.info("Webex bot successfully joined meeting")
                        # Console logging disabled - only retrieve on success if needed
                        # if hasattr(self, 'get_browser_console_logs'):
                        #     self.get_browser_console_logs()
                        return
                    
                    elif status == "LEFT" or status == "MEETING_ENDED":
                        # Bot was in meeting but meeting ended
                        logger.info(f"Webex meeting ended/left: {message}")
                        return  # Let the framework handle meeting_ended via WebSocket
                    
                    elif status == "WAITING_IN_LOBBY":
                        if waiting_room_start is None:
                            waiting_room_start = time.time()
                            logger.info("Bot in lobby, waiting for host to admit...")
                        else:
                            waiting_time = time.time() - waiting_room_start
                            if waiting_time > max_waiting_room_time:
                                logger.error(f"Timeout in waiting room after {waiting_time:.0f}s")
                                # Console logging disabled to reduce verbosity
                                # if hasattr(self, 'print_browser_console_logs'):
                                #     self.print_browser_console_logs()
                                raise UiWebexWaitingRoomTimeoutException(
                                    f"Timeout waiting in lobby after {waiting_time:.0f} seconds - host did not admit bot"
                                )
                            
                            # Log every 15 seconds
                            current_time = time.time()
                            if current_time - last_lobby_log_time >= 15:
                                logger.info(f"Still waiting in lobby ({waiting_time:.0f}s elapsed)")
                                last_lobby_log_time = current_time
                    
                    elif status in ("error", "FAILED_TO_JOIN"):
                        error_message = join_status.get("message", "Unknown error")
                        error_type = join_status.get("type", "unknown")
                        error_code = join_status.get("code")
                        
                        logger.error(f"Webex join error: {error_type} - {error_message} (code: {error_code})")
                        
                        # Console logging disabled to reduce verbosity
                        # if hasattr(self, 'print_browser_console_logs'):
                        #     self.print_browser_console_logs()
                        
                        # Admission timeout / denied
                        if error_code == 'ADMISSION_TIMEOUT' or (error_message and 'timeout waiting for admission' in error_message.lower()):
                            raise UiWebexWaitingRoomTimeoutException(f"Timeout waiting for admission: {error_message}")
                        
                        if error_code in ('GUEST_DENIED', 'DENIED', 'REJECTED', 'REMOVED'):
                            raise UiRequestToJoinDeniedException(f"Join request denied: {error_message}")
                        
                        # Auth failures
                        if error_code in (401, 403) or (error_type and "auth" in error_type.lower()):
                            raise UiWebexAuthFailedException(f"Authentication failed: {error_message}")
                        
                        # Meeting not found
                        if error_code == 404 or (error_message and "not found" in error_message.lower()):
                            raise UiWebexMeetingNotFoundException(f"Meeting not found: {error_message}")
                        
                        # Password issues
                        if error_code == 423 or (error_message and "password" in error_message.lower()):
                            raise UiWebexInvalidPasswordException(f"Invalid password: {error_message}")
                        
                        # SDK init failures
                        if error_type and ("sdk" in error_type.lower() or "init" in error_type.lower()):
                            raise UiWebexSDKInitFailedException(f"SDK initialization failed: {error_message}")
                        
                        # Generic join failure - infinitely retryable
                        raise UiWebexJoinFailedException(f"Join failed: {error_message}")
                    
                    elif status in ("joining", "JOINING", "initializing", "auto_joining"):
                        logger.debug(f"Join in progress: {status} - {message}")
                
                # Alternate detection: check meeting state directly
                meeting_state = self.driver.execute_script(
                    "return window.meeting ? window.meeting.state : null;"
                )
                
                if meeting_state == 'JOINED':
                    # Double-check it's not in lobby
                    is_in_lobby = self.driver.execute_script(
                        "return window.meeting ? window.meeting.isInLobby : false;"
                    )
                    if not is_in_lobby:
                        logger.info("Webex meeting JOINED (detected via meeting.state)")
                        # Console logging disabled to reduce verbosity
                        # if hasattr(self, 'get_browser_console_logs'):
                        #     self.get_browser_console_logs()
                        return
                
                time.sleep(check_interval)
                
            except Exception as e:
                if isinstance(e, (UiWebexAuthFailedException, UiWebexMeetingNotFoundException,
                                UiWebexInvalidPasswordException, UiWebexSDKInitFailedException,
                                UiWebexJoinFailedException, UiWebexWaitingRoomTimeoutException,
                                UiRequestToJoinDeniedException)):
                    raise
                
                logger.debug(f"Error checking Webex join status: {e}")
                time.sleep(check_interval)
        
        # Timeout
        logger.error(f"Timeout waiting for Webex join after {max_wait_time}s")
        # Console logging disabled to reduce verbosity
        # if hasattr(self, 'print_browser_console_logs'):
        #     self.print_browser_console_logs()
        raise UiWebexJoinFailedException(f"Timeout waiting for Webex meeting join after {max_wait_time} seconds")

    def check_for_waiting_room(self):
        """
        Check if bot is in a waiting room.

        Returns
        -------
        bool
            True if in waiting room, False otherwise
        """
        try:
            state = self.driver.execute_script("return window.webexState;")
            if state and state.get("inWaitingRoom"):
                return True
        except Exception as e:
            logger.debug(f"Error checking waiting room state: {e}")

        return False

    def is_meeting_active(self):
        """
        Check if the meeting is currently active.

        Returns
        -------
        bool
            True if meeting is active, False otherwise
        """
        try:
            state = self.driver.execute_script("return window.webexState;")
            if state and state.get("status") == "joined":
                # Check if meeting object exists and is connected
                is_connected = self.driver.execute_script(
                    "return window.meeting && window.meeting.isActive;"
                )
                return bool(is_connected)
        except Exception as e:
            logger.debug(f"Error checking meeting active state: {e}")

        return False

    def get_participant_count(self):
        """
        Get the current number of participants in the meeting.

        Returns
        -------
        int
            Number of participants, or 0 if unable to determine
        """
        try:
            count = self.driver.execute_script("""
                if (window.meeting && window.meeting.members) {
                    const members = window.meeting.members.membersCollection.members;
                    const membersList = Array.isArray(members) ? members : Object.values(members);
                    return membersList.filter(m => m && m.isInMeeting).length;
                }
                return 0;
            """)
            return int(count) if count else 0
        except Exception as e:
            logger.debug(f"Error getting participant count: {e}")
            return 0

    def enable_transcription(self):
        """
        Attempt to enable transcription for the meeting.

        Returns
        -------
        bool
            True if transcription was enabled, False otherwise
        """
        try:
            # Request transcription through the SDK
            result = self.driver.execute_script("""
                if (window.meeting && window.meeting.receiveTranscription) {
                    return window.meeting.receiveTranscription.start()
                        .then(() => true)
                        .catch(err => {
                            console.error('Failed to start transcription:', err);
                            return false;
                        });
                }
                return false;
            """)

            if result:
                logger.info("Transcription enabled successfully")
            else:
                logger.warning("Could not enable transcription")

            return bool(result)

        except Exception as e:
            logger.warning(f"Error enabling transcription: {e}")
            return False

    def leave_meeting(self):
        """
        Leave the Webex meeting gracefully.
        
        This calls the global leaveMeeting() function exposed by app.js,
        which handles:
        1. Sending meeting_ended to Python adapter (via centralized handleMeetingEnd)
        2. Disabling media sending and stopping all stream recorders
        3. Clearing tracking maps
        4. Calling meeting.leave() via SDK
        5. Waiting for WebSocket buffers to flush
        
        Uses execute_async_script to properly await the async function,
        with a fallback to fire-and-forget if async execution fails.
        """
        try:
            logger.info("Leaving Webex meeting via SDK")
            # Check if there's a meeting to leave
            has_meeting = self.driver.execute_script("return !!window.meeting;")
            if not has_meeting:
                logger.info("No meeting object found, nothing to leave")
                return

            # Try async execution first (properly waits for leaveMeeting to complete)
            self.driver.set_script_timeout(10)
            try:
                result = self.driver.execute_async_script("""
                    var callback = arguments[arguments.length - 1];
                    if (window.leaveMeeting) {
                        window.leaveMeeting()
                            .then(function() { callback('left'); })
                            .catch(function(err) { callback('error: ' + err.message); });
                    } else if (window.meeting) {
                        if (window.ws) {
                            window.ws.disableMediaSending();
                        }
                        window.meeting.leave()
                            .then(function() { callback('left'); })
                            .catch(function(err) { callback('error: ' + err.message); });
                    } else {
                        callback('no_meeting');
                    }
                """)
                logger.info(f"Leave meeting result: {result}")
            except Exception as async_err:
                logger.warning(f"Async leave failed ({async_err}), trying sync fallback")
                self.driver.execute_script("""
                    if (window.leaveMeeting) {
                        window.leaveMeeting();
                    } else if (window.meeting) {
                        if (window.ws) {
                            window.ws.disableMediaSending();
                        }
                        window.meeting.leave()
                            .then(() => console.log('[Webex] Left meeting'))
                            .catch(err => console.error('[Webex] Error leaving:', err));
                    }
                """)
                time.sleep(3)  # Give async operations time to complete
        except Exception as e:
            logger.warning(f"Error leaving meeting: {e}")

    def click_leave_button(self):
        """
        Alias for leave_meeting() to match framework's expected method name.
        The base WebBotAdapter.leave() calls this method.
        """
        self.leave_meeting()

    def dump_page_state(self):
        """
        Dump the current page state for debugging.
        Returns information about window objects, errors, and state.
        """
        try:
            state_info = self.driver.execute_script("""
                const state = {
                    url: window.location.href,
                    webexInitialData: window.webexInitialData || null,
                    webexJoinStatus: window.webexJoinStatus || null,
                    hasWebex: typeof window.Webex !== 'undefined',
                    webexInstance: !!window.webex,
                    hasMeeting: !!window.meeting,
                    meetingActive: window.meeting ? window.meeting.isActive : false,
                    meetingState: window.meeting ? window.meeting.state : null,
                    meetingEndedSent: typeof meetingEndedSent !== 'undefined' ? meetingEndedSent : null,
                    botAdmittedToMeeting: typeof botAdmittedToMeeting !== 'undefined' ? botAdmittedToMeeting : null,
                    hasWebSocket: !!window.websocket,
                    hasWsWrapper: !!window.ws,
                    websocketReadyState: window.ws && window.ws.readyState !== undefined ? window.ws.readyState : (window.websocket && window.websocket.ws ? window.websocket.ws.readyState : null),
                    errors: []
                };
                
                // Try to get any stored errors
                if (window.lastError) {
                    state.errors.push({
                        type: 'lastError',
                        message: window.lastError.message || String(window.lastError),
                        stack: window.lastError.stack || null
                    });
                }
                
                return state;
            """)
            
            logger.info("=" * 80)
            logger.info("PAGE STATE DUMP")
            logger.info("=" * 80)
            logger.info(f"URL: {state_info.get('url')}")
            logger.info(f"webexInitialData present: {state_info.get('webexInitialData') is not None}")
            if state_info.get('webexInitialData'):
                # Don't log the access token
                data = state_info.get('webexInitialData').copy()
                if 'accessToken' in data:
                    data['accessToken'] = '***REDACTED***'
                logger.info(f"webexInitialData: {data}")
            logger.info(f"webexJoinStatus: {state_info.get('webexJoinStatus')}")
            logger.info(f"Has Webex SDK: {state_info.get('hasWebex')}")
            logger.info(f"Webex instance exists: {state_info.get('webexInstance')}")
            logger.info(f"Meeting exists: {state_info.get('hasMeeting')}")
            logger.info(f"Meeting active: {state_info.get('meetingActive')}")
            logger.info(f"Meeting state: {state_info.get('meetingState')}")
            logger.info(f"Meeting ended sent: {state_info.get('meetingEndedSent')}")
            logger.info(f"Bot admitted to meeting: {state_info.get('botAdmittedToMeeting')}")
            logger.info(f"WebSocket wrapper exists: {state_info.get('hasWsWrapper')}")
            logger.info(f"WebSocket exists: {state_info.get('hasWebSocket')}")
            logger.info(f"WebSocket readyState: {state_info.get('websocketReadyState')}")
            
            if state_info.get('errors'):
                logger.info("Errors found:")
                for error in state_info['errors']:
                    logger.error(f"  {error['type']}: {error['message']}")
                    if error.get('stack'):
                        logger.error(f"  Stack: {error['stack']}")
            
            logger.info("=" * 80)
            
            return state_info
            
        except Exception as e:
            logger.error(f"Error dumping page state: {e}")
            return None

