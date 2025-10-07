import base64
import json
import threading
import time
from unittest.mock import ANY, MagicMock, call, patch

import zoom_meeting_sdk as zoom
from django.db import connection
from django.test import override_settings, tag
from django.test.testcases import TransactionTestCase

from bots.automatic_leave_configuration import AutomaticLeaveConfiguration
from bots.bot_controller import BotController
from bots.bot_controller.file_uploader import FileUploader
from bots.bot_controller.pipeline_configuration import PipelineConfiguration
from bots.bots_api_views import send_sync_command
from bots.models import (
    Bot,
    BotChatMessageRequest,
    BotChatMessageRequestStates,
    BotChatMessageToOptions,
    BotEventManager,
    BotEventSubTypes,
    BotEventTypes,
    BotMediaRequest,
    BotMediaRequestMediaTypes,
    BotMediaRequestStates,
    BotStates,
    ChatMessage,
    ChatMessageToOptions,
    Credentials,
    CreditTransaction,
    MediaBlob,
    Organization,
    ParticipantEvent,
    ParticipantEventTypes,
    Project,
    Recording,
    RecordingFormats,
    RecordingStates,
    RecordingTranscriptionStates,
    RecordingTypes,
    TranscriptionFailureReasons,
    TranscriptionProviders,
    TranscriptionTypes,
)
from bots.utils import mp3_to_pcm, png_to_yuv420_frame, scale_i420

from .mock_data import MockPCMAudioFrame, MockVideoFrame


def mock_file_field_delete_sets_name_to_none(instance, save=True):
    """
    A side_effect function for mocking FieldFile.delete.
    Sets the FieldFile's name to None and saves the parent model instance.
    """
    # 'instance' here is the FieldFile instance being deleted
    instance.name = None
    if save:
        # instance.instance refers to the model instance (e.g., Recording)
        # that owns this FieldFile.
        instance.instance.save()


def create_mock_file_uploader():
    mock_file_uploader = MagicMock(spec=FileUploader)
    mock_file_uploader.upload_file.return_value = None
    mock_file_uploader.wait_for_upload.return_value = None
    mock_file_uploader.delete_file.return_value = None
    mock_file_uploader.key = "test-recording-key"  # Simple string attribute
    return mock_file_uploader


def create_mock_zoom_sdk():
    # Create mock zoom_meeting_sdk module with proper callback handling
    base_mock = MagicMock()

    class MeetingFailCode:
        MEETING_FAIL_BLOCKED_BY_ACCOUNT_ADMIN = "100"
        MEETING_FAIL_UNABLE_TO_JOIN_EXTERNAL_MEETING = zoom.MeetingFailCode.MEETING_FAIL_UNABLE_TO_JOIN_EXTERNAL_MEETING

    base_mock.MeetingFailCode = MeetingFailCode

    # Create a custom ZoomSDKRendererDelegateCallbacks class that actually stores the callback
    class MockZoomSDKRendererDelegateCallbacks:
        def __init__(
            self,
            onRawDataFrameReceivedCallback,
            onRendererBeDestroyedCallback,
            onRawDataStatusChangedCallback,
        ):
            self.stored_callback = onRawDataFrameReceivedCallback
            self.stored_renderer_destroyed_callback = onRendererBeDestroyedCallback
            self.stored_raw_data_status_changed_callback = onRawDataStatusChangedCallback

        def onRawDataFrameReceivedCallback(self, data):
            return self.stored_callback(data)

        def onRendererBeDestroyedCallback(self):
            return self.stored_renderer_destroyed_callback()

        def onRawDataStatusChangedCallback(self, status):
            return self.stored_raw_data_status_changed_callback(status)

    base_mock.ZoomSDKRendererDelegateCallbacks = MockZoomSDKRendererDelegateCallbacks

    # Create a custom MeetingRecordingCtrlEventCallbacks class that actually stores the callback
    class MockMeetingRecordingCtrlEventCallbacks:
        def __init__(self, onRecordPrivilegeChangedCallback, onLocalRecordingPrivilegeRequestStatusCallback):
            self.stored_callback = onRecordPrivilegeChangedCallback
            self.stored_local_recording_privilege_request_status_callback = onLocalRecordingPrivilegeRequestStatusCallback

        def onRecordPrivilegeChangedCallback(self, can_record):
            return self.stored_callback(can_record)

        def onLocalRecordingPrivilegeRequestStatusCallback(self, status):
            return self.stored_local_recording_privilege_request_status_callback(status)

    base_mock.MeetingRecordingCtrlEventCallbacks = MockMeetingRecordingCtrlEventCallbacks

    # Create a custom AuthServiceEventCallbacks class that actually stores the callback
    class MockAuthServiceEventCallbacks:
        def __init__(self, onAuthenticationReturnCallback):
            self.stored_callback = onAuthenticationReturnCallback

        def onAuthenticationReturnCallback(self, result):
            return self.stored_callback(result)

    # Replace the mock's AuthServiceEventCallbacks with our custom version
    base_mock.AuthServiceEventCallbacks = MockAuthServiceEventCallbacks

    # Create a custom MeetingServiceEventCallbacks class that actually stores the callback
    class MockMeetingServiceEventCallbacks:
        def __init__(self, onMeetingStatusChangedCallback):
            self.stored_callback = onMeetingStatusChangedCallback

        def onMeetingStatusChangedCallback(self, status, result):
            return self.stored_callback(status, result)

    # Replace the mock's MeetingServiceEventCallbacks with our custom version
    base_mock.MeetingServiceEventCallbacks = MockMeetingServiceEventCallbacks

    # Create a custom GetRawdataVideoSourceHelper class that actually stores the callback
    class MockGetRawdataVideoSourceHelper:
        def __init__(self):
            pass

        def setExternalVideoSource(self, video_source):
            return zoom.SDKError.SDKERR_SUCCESS

    base_mock.GetRawdataVideoSourceHelper = MockGetRawdataVideoSourceHelper

    # Set up constants
    base_mock.SDKERR_SUCCESS = zoom.SDKError.SDKERR_SUCCESS
    base_mock.AUTHRET_SUCCESS = zoom.AuthResult.AUTHRET_SUCCESS
    base_mock.MEETING_STATUS_IDLE = zoom.MeetingStatus.MEETING_STATUS_IDLE
    base_mock.MEETING_STATUS_CONNECTING = zoom.MeetingStatus.MEETING_STATUS_CONNECTING
    base_mock.MEETING_STATUS_INMEETING = zoom.MeetingStatus.MEETING_STATUS_INMEETING
    base_mock.MEETING_STATUS_ENDED = zoom.MeetingStatus.MEETING_STATUS_ENDED
    base_mock.LEAVE_MEETING = zoom.LeaveMeetingCmd.LEAVE_MEETING
    base_mock.AUTHRET_JWTTOKENWRONG = zoom.AuthResult.AUTHRET_JWTTOKENWRONG

    # Mock SDK_LANGUAGE_ID
    base_mock.SDK_LANGUAGE_ID = MagicMock()
    base_mock.SDK_LANGUAGE_ID.LANGUAGE_English = zoom.SDK_LANGUAGE_ID.LANGUAGE_English

    # Mock SDKAudioChannel
    base_mock.ZoomSDKAudioChannel_Mono = zoom.ZoomSDKAudioChannel.ZoomSDKAudioChannel_Mono

    # Mock SDKUserType
    base_mock.SDKUserType = MagicMock()
    base_mock.SDKUserType.SDK_UT_WITHOUT_LOGIN = zoom.SDKUserType.SDK_UT_WITHOUT_LOGIN

    # Create mock services
    mock_meeting_service = MagicMock()
    mock_auth_service = MagicMock()
    mock_setting_service = MagicMock()
    mock_zoom_sdk_renderer = MagicMock()

    # Configure mock services
    mock_meeting_service.SetEvent.return_value = base_mock.SDKERR_SUCCESS
    mock_meeting_service.Join.return_value = base_mock.SDKERR_SUCCESS
    mock_meeting_service.GetMeetingStatus.return_value = base_mock.MEETING_STATUS_IDLE
    mock_meeting_service.Leave.return_value = base_mock.SDKERR_SUCCESS

    mock_meeting_video_controller = MagicMock()
    mock_meeting_video_controller.UnmuteVideo.return_value = base_mock.SDKERR_SUCCESS
    mock_meeting_service.GetMeetingVideoController.return_value = mock_meeting_video_controller

    # Add mock recording controller
    mock_recording_controller = MagicMock()
    mock_recording_controller.CanStartRawRecording.return_value = base_mock.SDKERR_SUCCESS
    mock_recording_controller.StartRawRecording.return_value = base_mock.SDKERR_SUCCESS
    mock_recording_controller.StopRawRecording.return_value = base_mock.SDKERR_SUCCESS
    mock_meeting_service.GetMeetingRecordingController.return_value = mock_recording_controller

    mock_auth_service.SetEvent.return_value = base_mock.SDKERR_SUCCESS
    mock_auth_service.SDKAuth.return_value = base_mock.SDKERR_SUCCESS

    # Configure service creation functions
    base_mock.CreateMeetingService.return_value = mock_meeting_service
    base_mock.CreateAuthService.return_value = mock_auth_service
    base_mock.CreateSettingService.return_value = mock_setting_service
    base_mock.CreateRenderer.return_value = mock_zoom_sdk_renderer

    # Configure InitSDK
    base_mock.InitSDK.return_value = base_mock.SDKERR_SUCCESS

    # Add SDKError class mock with SDKERR_SUCCESS
    base_mock.SDKError = MagicMock()
    base_mock.SDKError.SDKERR_SUCCESS = zoom.SDKError.SDKERR_SUCCESS
    base_mock.SDKError.SDKERR_INTERNAL_ERROR = zoom.SDKError.SDKERR_INTERNAL_ERROR

    # Create a mock PerformanceData class
    class MockPerformanceData:
        def __init__(self):
            self.totalProcessingTimeMicroseconds = 1000
            self.numCalls = 100
            self.maxProcessingTimeMicroseconds = 20
            self.minProcessingTimeMicroseconds = 5
            self.processingTimeBinMin = 0
            self.processingTimeBinMax = 100
            self.processingTimeBinCounts = [
                10,
                20,
                30,
                20,
                10,
                5,
                3,
                2,
            ]  # Example distribution

    # Create a custom ZoomSDKAudioRawDataDelegateCallbacks class that actually stores the callback
    class MockZoomSDKAudioRawDataDelegateCallbacks:
        def __init__(
            self,
            onOneWayAudioRawDataReceivedCallback,
            onMixedAudioRawDataReceivedCallback,
            collectPerformanceData=False,
        ):
            self.stored_one_way_callback = onOneWayAudioRawDataReceivedCallback
            self.stored_mixed_callback = onMixedAudioRawDataReceivedCallback
            self.collect_performance_data = collectPerformanceData

        def onOneWayAudioRawDataReceivedCallback(self, data, node_id):
            return self.stored_one_way_callback(data, node_id)

        def onMixedAudioRawDataReceivedCallback(self, data):
            return self.stored_mixed_callback(data)

        def getPerformanceData(self):
            return MockPerformanceData()

    base_mock.ZoomSDKAudioRawDataDelegateCallbacks = MockZoomSDKAudioRawDataDelegateCallbacks

    class MockZoomSDKVirtualAudioMicEventCallbacks:
        def __init__(self, onMicInitializeCallback, onMicStartSendCallback):
            self.stored_initialize_callback = onMicInitializeCallback
            self.stored_start_send_callback = onMicStartSendCallback

        def onMicInitializeCallback(self, sender):
            return self.stored_initialize_callback(sender)

        def onMicStartSendCallback(self):
            return self.stored_start_send_callback()

    base_mock.ZoomSDKVirtualAudioMicEventCallbacks = MockZoomSDKVirtualAudioMicEventCallbacks

    class MockZoomSDKVideoSourceCallbacks:
        def __init__(self, onInitializeCallback, onStartSendCallback):
            self.stored_initialize_callback = onInitializeCallback
            self.stored_start_send_callback = onStartSendCallback

        def onInitializeCallback(self, sender, support_cap_list, suggest_cap):
            return self.stored_initialize_callback(sender, support_cap_list, suggest_cap)

        def onStartSendCallback(self):
            return self.stored_start_send_callback()

    base_mock.ZoomSDKVideoSourceCallbacks = MockZoomSDKVideoSourceCallbacks

    # Create a mock participant class
    class MockParticipant:
        def __init__(self, user_id, user_name, persistent_id):
            self._user_id = user_id
            self._user_name = user_name
            self._persistent_id = persistent_id
            self._is_host = False

        def GetUserID(self):
            return self._user_id

        def GetUserName(self):
            return self._user_name

        def GetPersistentId(self):
            return self._persistent_id

        def IsHost(self):
            return self._is_host

    # Create a mock participants controller
    mock_participants_controller = MagicMock()
    mock_participants_controller.GetParticipantsList.return_value = [1, 2]  # Return test user ID
    mock_participants_controller.GetUserByUserID.side_effect = lambda user_id: (MockParticipant(1, "Bot User", "bot_persistent_id") if user_id == 1 else MockParticipant(2, "Test User", "test_persistent_id_123") if user_id == 2 else None)
    mock_participants_controller.GetMySelfUser.return_value = MockParticipant(1, "Bot User", "bot_persistent_id")

    # Add participants controller to meeting service
    mock_meeting_service.GetMeetingParticipantsController.return_value = mock_participants_controller

    return base_mock


def create_mock_deepgram():
    # Create mock objects
    mock_deepgram = MagicMock()
    mock_response = MagicMock()
    mock_results = MagicMock()
    mock_channel = MagicMock()
    mock_alternative = MagicMock()

    # Set up the mock response structure
    mock_alternative.to_json.return_value = json.dumps(
        {
            "transcript": "This is a test transcript",
            "confidence": 0.95,
            "words": [
                {"word": "This", "start": 0.0, "end": 0.2, "confidence": 0.98},
                {"word": "is", "start": 0.2, "end": 0.4, "confidence": 0.97},
                {"word": "a", "start": 0.4, "end": 0.5, "confidence": 0.99},
                {"word": "test", "start": 0.5, "end": 0.8, "confidence": 0.96},
                {"word": "transcript", "start": 0.8, "end": 1.2, "confidence": 0.94},
            ],
        }
    )
    mock_channel.alternatives = [mock_alternative]
    mock_results.channels = [mock_channel]
    mock_response.results = mock_results

    # Set up the mock client
    mock_deepgram.listen.rest.v.return_value.transcribe_file.return_value = mock_response
    return mock_deepgram


@tag("zoom_tests")
class TestZoomBot(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Instead of setting environment variables directly:
        # os.environ["AWS_RECORDING_STORAGE_BUCKET_NAME"] = "test-bucket"
        # os.environ["CHARGE_CREDITS_FOR_BOTS"] = "true"

        # The settings have already been loaded, so we need to override them
        # These will be applied to all tests in this class
        cls.settings_override = override_settings(AWS_RECORDING_STORAGE_BUCKET_NAME="test-bucket", CHARGE_CREDITS_FOR_BOTS=True)
        cls.settings_override.enable()

    @classmethod
    def tearDownClass(cls):
        # Clean up the settings override when done
        cls.settings_override.disable()
        super().tearDownClass()

    def setUp(self):
        # Recreate organization and project for each test
        self.organization = Organization.objects.create(name="Test Org")
        self.project = Project.objects.create(name="Test Project", organization=self.organization)

        # Recreate credentials
        self.credentials = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.ZOOM_OAUTH)
        self.credentials.set_credentials({"client_id": "test_client_id", "client_secret": "test_client_secret"})
        self.deepgram_credentials = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.DEEPGRAM)
        self.deepgram_credentials.set_credentials({"api_key": "test_api_key"})
        self.google_credentials = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.GOOGLE_TTS)
        self.google_credentials.set_credentials({"service_account_json": '{"type": "service_account", "project_id": "test-project", "private_key_id": "test-private-key-id", "private_key": "test-private-key", "client_email": "test-client-email", "client_id": "test-client-id", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token", "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs", "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/test-client-email"}'})

        # Create a bot for each test
        self.bot = Bot.objects.create(
            project=self.project,
            name="Test Bot",
            meeting_url="https://zoom.us/j/123456789?pwd=password123",
        )

        # Create default recording
        self.recording = Recording.objects.create(
            bot=self.bot,
            recording_type=RecordingTypes.AUDIO_AND_VIDEO,
            transcription_type=TranscriptionTypes.NON_REALTIME,
            transcription_provider=TranscriptionProviders.DEEPGRAM,
            is_default_recording=True,
        )

        # Try to transition the state from READY to JOINING
        BotEventManager.create_event(self.bot, BotEventTypes.JOIN_REQUESTED)

        self.test_mp3_bytes = base64.b64decode("SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU2LjM2LjEwMAAAAAAAAAAAAAAA//OEAAAAAAAAAAAAAAAAAAAAAAAASW5mbwAAAA8AAAAEAAABIADAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV6urq6urq6urq6urq6urq6urq6urq6urq6v////////////////////////////////8AAAAATGF2YzU2LjQxAAAAAAAAAAAAAAAAJAAAAAAAAAAAASDs90hvAAAAAAAAAAAAAAAAAAAA//MUZAAAAAGkAAAAAAAAA0gAAAAATEFN//MUZAMAAAGkAAAAAAAAA0gAAAAARTMu//MUZAYAAAGkAAAAAAAAA0gAAAAAOTku//MUZAkAAAGkAAAAAAAAA0gAAAAANVVV")
        self.test_png_bytes = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAMAAAADCAYAAABWKLW/AAAAEklEQVR42mNk+P+/ngEKGHFyAK2mB3vQeaNWAAAAAElFTkSuQmCC")

        self.audio_blob = MediaBlob.get_or_create_from_blob(project=self.bot.project, blob=self.test_mp3_bytes, content_type="audio/mp3")

        self.image_blob = MediaBlob.get_or_create_from_blob(project=self.bot.project, blob=self.test_png_bytes, content_type="image/png")

        # Configure Celery to run tasks eagerly (synchronously)
        from django.conf import settings

        settings.CELERY_TASK_ALWAYS_EAGER = True
        settings.CELERY_TASK_EAGER_PROPAGATES = True

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("deepgram.DeepgramClient")
    def test_bot_can_wait_for_host_then_join_meeting(
        self,
        MockDeepgramClient,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        # Set up Deepgram mock
        MockDeepgramClient.return_value = create_mock_deepgram()

        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Create bot controller with a very short wait time
        controller = BotController(self.bot.id)
        controller.automatic_leave_configuration = AutomaticLeaveConfiguration(wait_for_host_to_start_meeting_timeout_seconds=2)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_join_flow():
            adapter = controller.adapter
            # Simulate successful auth
            adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)

            # Configure GetMeetingStatus to return waiting for host status
            adapter.meeting_service.GetMeetingStatus.return_value = mock_zoom_sdk_adapter.MEETING_STATUS_WAITINGFORHOST

            # Simulate waiting for host
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_WAITINGFORHOST,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Sleep for 1 second (less than the timeout)
            time.sleep(1)

            # Update GetMeetingStatus to return connecting status
            adapter.meeting_service.GetMeetingStatus.return_value = mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING

            # Simulate connecting
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Update GetMeetingStatus to return in-meeting status
            adapter.meeting_service.GetMeetingStatus.return_value = mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING

            # Simulate successful join
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Wait for the video input manager to be set up
            time.sleep(2)

            # Simulate meeting ended
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_ENDED,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Clean up connections in thread
            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(2, simulate_join_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=10)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Assert that the bot is in the ENDED state
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Verify all bot events in sequence
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 5)  # We expect 5 events in total

        # Verify join_requested_event (Event 1)
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)

        # Verify bot_joined_meeting_event (Event 2)
        bot_joined_meeting_event = bot_events[1]
        self.assertEqual(bot_joined_meeting_event.event_type, BotEventTypes.BOT_JOINED_MEETING)
        self.assertEqual(bot_joined_meeting_event.old_state, BotStates.JOINING)
        self.assertEqual(bot_joined_meeting_event.new_state, BotStates.JOINED_NOT_RECORDING)

        # Verify recording_permission_granted_event (Event 3)
        recording_permission_granted_event = bot_events[2]
        self.assertEqual(recording_permission_granted_event.event_type, BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED)
        self.assertEqual(recording_permission_granted_event.old_state, BotStates.JOINED_NOT_RECORDING)
        self.assertEqual(recording_permission_granted_event.new_state, BotStates.JOINED_RECORDING)

        # Verify meeting_ended_event (Event 4)
        meeting_ended_event = bot_events[3]
        self.assertEqual(meeting_ended_event.event_type, BotEventTypes.MEETING_ENDED)
        self.assertEqual(meeting_ended_event.old_state, BotStates.JOINED_RECORDING)
        self.assertEqual(meeting_ended_event.new_state, BotStates.POST_PROCESSING)

        # Verify post_processing_completed_event (Event 5)
        post_processing_completed_event = bot_events[4]
        self.assertEqual(post_processing_completed_event.event_type, BotEventTypes.POST_PROCESSING_COMPLETED)
        self.assertEqual(post_processing_completed_event.old_state, BotStates.POST_PROCESSING)
        self.assertEqual(post_processing_completed_event.new_state, BotStates.ENDED)

        # Verify that a charge was created
        credit_transaction = CreditTransaction.objects.filter(bot=self.bot).first()
        self.assertIsNotNone(credit_transaction, "No credit transaction was created for the bot")
        self.assertEqual(credit_transaction.organization, self.organization)
        self.assertLess(credit_transaction.centicredits_delta, 0, "Credit transaction should have a negative delta (charge)")
        self.assertEqual(credit_transaction.centicredits_delta, -self.bot.centicredits_consumed(), "Credit transaction should have a negative delta (charge)")
        self.assertEqual(credit_transaction.bot, self.bot)
        self.assertEqual(credit_transaction.organization.centicredits, 500 - self.bot.centicredits_consumed())

        # Verify expected SDK calls
        mock_zoom_sdk_adapter.InitSDK.assert_called_once()
        mock_zoom_sdk_adapter.CreateMeetingService.assert_called_once()
        mock_zoom_sdk_adapter.CreateAuthService.assert_called_once()
        controller.adapter.meeting_service.Join.assert_called_once()

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("deepgram.DeepgramClient")
    @patch("time.time")
    def test_bot_auto_leaves_meeting_after_silence_timeout(
        self,
        mock_time,
        MockDeepgramClient,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        # Set up Deepgram mock
        MockDeepgramClient.return_value = create_mock_deepgram()

        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Set initial time
        current_time = 1000.0
        mock_time.return_value = current_time

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_join_flow():
            adapter = controller.adapter
            # Simulate successful auth
            adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)

            # Configure GetMeetingStatus to return the correct status
            adapter.meeting_service.GetMeetingStatus.return_value = mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING

            # Simulate connecting
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Update GetMeetingStatus to return in-meeting status
            adapter.meeting_service.GetMeetingStatus.return_value = mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING

            # Simulate successful join
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Wait for the video input manager to be set up
            time.sleep(2)

            # Simulate receiving some initial audio
            adapter.audio_source.onOneWayAudioRawDataReceivedCallback(
                MockPCMAudioFrame(),
                2,  # Simulated participant ID that's not the bot
            )

            # Advance time past silence activation threshold (1200 seconds)
            nonlocal current_time
            current_time += 1201
            mock_time.return_value = current_time

            # Trigger check of auto-leave conditions which should activate silence detection
            adapter.check_auto_leave_conditions()

            current_time += 601
            mock_time.return_value = current_time

            # Trigger check of auto-leave conditions which should trigger auto-leave
            adapter.check_auto_leave_conditions()

            # Sleep to allow for event processing
            time.sleep(2)

            # Update GetMeetingStatus to return ended status when meeting ends
            adapter.meeting_service.GetMeetingStatus.return_value = mock_zoom_sdk_adapter.MEETING_STATUS_ENDED

            # Simulate meeting ended after auto-leave
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_ENDED,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Clean up connections in thread
            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(2, simulate_join_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=15)

        # Refresh the bot from the database
        time.sleep(3)
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Assert that the bot is in the ENDED state
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Assert that silence detection was activated
        self.assertTrue(controller.adapter.silence_detection_activated)
        self.assertIsNotNone(controller.adapter.joined_at)

        # Verify bot events in sequence
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 6)  # We expect 6 events in total

        # Verify join_requested_event (Event 1)
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)

        # Verify bot_joined_meeting_event (Event 2)
        bot_joined_meeting_event = bot_events[1]
        self.assertEqual(bot_joined_meeting_event.event_type, BotEventTypes.BOT_JOINED_MEETING)
        self.assertEqual(bot_joined_meeting_event.old_state, BotStates.JOINING)
        self.assertEqual(bot_joined_meeting_event.new_state, BotStates.JOINED_NOT_RECORDING)

        # Verify recording_permission_granted_event (Event 3)
        recording_permission_granted_event = bot_events[2]
        self.assertEqual(
            recording_permission_granted_event.event_type,
            BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED,
        )
        self.assertEqual(recording_permission_granted_event.old_state, BotStates.JOINED_NOT_RECORDING)
        self.assertEqual(recording_permission_granted_event.new_state, BotStates.JOINED_RECORDING)

        # Verify leave_requested_event (Event 4)
        leave_requested_event = bot_events[3]
        self.assertEqual(leave_requested_event.event_type, BotEventTypes.LEAVE_REQUESTED)
        self.assertEqual(leave_requested_event.old_state, BotStates.JOINED_RECORDING)
        self.assertEqual(leave_requested_event.new_state, BotStates.LEAVING)
        self.assertEqual(
            leave_requested_event.event_sub_type,
            BotEventSubTypes.LEAVE_REQUESTED_AUTO_LEAVE_SILENCE,
        )

        # Verify bot_left_meeting_event (Event 5)
        bot_left_meeting_event = bot_events[4]
        self.assertEqual(bot_left_meeting_event.event_type, BotEventTypes.BOT_LEFT_MEETING)
        self.assertEqual(bot_left_meeting_event.old_state, BotStates.LEAVING)
        self.assertEqual(bot_left_meeting_event.new_state, BotStates.POST_PROCESSING)
        self.assertIsNone(bot_left_meeting_event.event_sub_type)

        # Verify post_processing_completed_event (Event 6)
        post_processing_completed_event = bot_events[5]
        self.assertEqual(post_processing_completed_event.event_type, BotEventTypes.POST_PROCESSING_COMPLETED)
        self.assertEqual(post_processing_completed_event.old_state, BotStates.POST_PROCESSING)
        self.assertEqual(post_processing_completed_event.new_state, BotStates.ENDED)

        # Verify that the adapter's leave method was called with the correct reason
        controller.adapter.meeting_service.Leave.assert_called_once_with(mock_zoom_sdk_adapter.LEAVE_MEETING)

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("deepgram.DeepgramClient")
    @patch("google.cloud.texttospeech.TextToSpeechClient")
    @patch("django.db.models.fields.files.FieldFile.delete", autospec=True)
    def test_bot_can_join_meeting_and_record_audio_and_video(
        self,
        mock_delete_file_field,
        MockTextToSpeechClient,
        MockDeepgramClient,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        mock_delete_file_field.side_effect = mock_file_field_delete_sets_name_to_none

        self.bot.settings = {
            "recording_settings": {
                "format": RecordingFormats.MP4,
            }
        }
        self.bot.save()

        # Set up Google TTS mock
        mock_tts_client = MagicMock()
        mock_tts_response = MagicMock()

        # Create fake PCM audio data (1 second of 44.1kHz audio)
        # WAV header (44 bytes) + PCM data
        wav_header = (
            b"RIFF"  # ChunkID (4 bytes)
            b"\x24\x00\x00\x00"  # ChunkSize (4 bytes)
            b"WAVE"  # Format (4 bytes)
            b"fmt "  # Subchunk1ID (4 bytes)
            b"\x10\x00\x00\x00"  # Subchunk1Size (4 bytes)
            b"\x01\x00"  # AudioFormat (2 bytes)
            b"\x01\x00"  # NumChannels (2 bytes)
            b"\x44\xac\x00\x00"  # SampleRate (4 bytes)
            b"\x88\x58\x01\x00"  # ByteRate (4 bytes)
            b"\x02\x00"  # BlockAlign (2 bytes)
            b"\x10\x00"  # BitsPerSample (2 bytes)
            b"data"  # Subchunk2ID (4 bytes)
            b"\x50\x00\x00\x00"  # Subchunk2Size (4 bytes) - size of audio data
        )
        pcm_speech_data = b"\x00\x00" * (40)  # small period of silence at 44.1kHz
        mock_tts_response.audio_content = wav_header + pcm_speech_data

        # Configure the mock client to return our mock response
        mock_tts_client.synthesize_speech.return_value = mock_tts_response
        MockTextToSpeechClient.from_service_account_info.return_value = mock_tts_client

        # Set up Deepgram mock
        MockDeepgramClient.return_value = create_mock_deepgram()

        # Store uploaded data for verification
        uploaded_data = bytearray()

        # Configure the mock uploader to capture uploaded data
        mock_uploader = create_mock_file_uploader()

        def capture_upload_part(file_path):
            uploaded_data.extend(open(file_path, "rb").read())

        mock_uploader.upload_file.side_effect = capture_upload_part
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        audio_request = None
        image_request = None
        speech_request = None
        self.chat_message_request = None

        # Create a mock chat message info object
        mock_chat_msg_info = MagicMock()
        mock_chat_msg_info.GetContent.return_value = "Hello bot from Test User!"
        mock_chat_msg_info.GetSenderUserId.return_value = 2  # Test User's ID
        mock_chat_msg_info.GetTimeStamp.return_value = time.time()
        mock_chat_msg_info.GetMessageID.return_value = "test_chat_message_id_001"
        mock_chat_msg_info.IsChatToAllPanelist.return_value = False
        mock_chat_msg_info.IsChatToAll.return_value = False
        mock_chat_msg_info.IsChatToWaitingroom.return_value = False
        mock_chat_msg_info.IsComment.return_value = False
        mock_chat_msg_info.IsThread.return_value = False
        mock_chat_msg_info.GetThreadID.return_value = ""

        def simulate_join_flow():
            nonlocal audio_request, image_request, speech_request

            adapter = controller.adapter
            # Simulate successful auth
            adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)

            # Simulate connecting
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Simulate successful join
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Wait for the video input manager to be set up
            time.sleep(2)

            # Simulate video frame received
            adapter.video_input_manager.input_streams[0].renderer_delegate.onRawDataFrameReceivedCallback(MockVideoFrame())

            # Simulate audio frame received
            adapter.audio_source.onOneWayAudioRawDataReceivedCallback(
                MockPCMAudioFrame(),
                2,  # Simulated participant ID that's not the bot
            )
            adapter.audio_source.onMixedAudioRawDataReceivedCallback(MockPCMAudioFrame())

            # simulate audio mic initialized
            adapter.virtual_audio_mic_event_passthrough.onMicInitializeCallback(MagicMock())

            # simulate audio mic started
            adapter.virtual_audio_mic_event_passthrough.onMicStartSendCallback()

            # simulate video source initialized
            mock_suggest_cap = MagicMock()
            mock_suggest_cap.width = 640
            mock_suggest_cap.height = 480
            mock_suggest_cap.frame = 30
            adapter.virtual_camera_video_source.onInitializeCallback(MagicMock(), [], mock_suggest_cap)

            # simulate video source started
            adapter.virtual_camera_video_source.onStartSendCallback()

            # simulate sending audio and image
            # Create media requests
            audio_request = BotMediaRequest.objects.create(
                bot=self.bot,
                media_blob=self.audio_blob,
                media_type=BotMediaRequestMediaTypes.AUDIO,
            )

            image_request = BotMediaRequest.objects.create(
                bot=self.bot,
                media_blob=self.image_blob,
                media_type=BotMediaRequestMediaTypes.IMAGE,
            )

            send_sync_command(self.bot, "sync_media_requests")

            # Sleep to give audio output manager time to play the audio
            time.sleep(2.0)

            # Create text-to-speech request
            speech_request = BotMediaRequest.objects.create(
                bot=self.bot,
                text_to_speak="Hello, this is a test speech",
                text_to_speech_settings={
                    "google": {
                        "voice_language_code": "en-US",
                        "voice_name": "en-US-Standard-A",
                    }
                },
                media_type=BotMediaRequestMediaTypes.AUDIO,
            )

            send_sync_command(self.bot, "sync_media_requests")

            # Sleep to give audio output manager time to play the speech audio
            time.sleep(2.0)

            # Create chat message request
            self.chat_message_request = BotChatMessageRequest.objects.create(
                bot=self.bot,
                message="Hello from the bot!",
                to=BotChatMessageToOptions.EVERYONE,
            )

            send_sync_command(self.bot, "sync_chat_message_requests")

            # Sleep to give the bot time to send the chat message
            time.sleep(1.0)

            # Simulate chat message received
            adapter.on_chat_msg_notification_callback(mock_chat_msg_info, mock_chat_msg_info.GetContent())

            # Simulate user leaving
            adapter.on_user_left_callback([2], [])

            # Simulate meeting ended
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_ENDED,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(3, simulate_join_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=11)

        # Verify that we received some data
        self.assertGreater(len(uploaded_data), 100, "Uploaded data length is not correct")

        # Check for MP4 file signature (starts with 'ftyp')
        mp4_signature_found = b"ftyp" in uploaded_data[:1000]
        self.assertTrue(mp4_signature_found, "MP4 file signature not found in uploaded data")

        # Additional verification for FileUploader
        mock_uploader.upload_file.assert_called_once()
        self.assertGreater(mock_uploader.upload_file.call_count, 0, "upload_file was never called")
        mock_uploader.wait_for_upload.assert_called_once()
        mock_uploader.delete_file.assert_called_once()

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Assert that the bot is in the ENDED state
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Verify all bot events in sequence
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 5)  # We expect 5 events in total

        # Verify join_requested_event (Event 1)
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)
        self.assertIsNone(join_requested_event.event_sub_type)
        self.assertEqual(join_requested_event.metadata, {})
        self.assertIsNotNone(join_requested_event.requested_bot_action_taken_at)

        # Verify bot_joined_meeting_event (Event 2)
        bot_joined_meeting_event = bot_events[1]
        self.assertEqual(bot_joined_meeting_event.event_type, BotEventTypes.BOT_JOINED_MEETING)
        self.assertEqual(bot_joined_meeting_event.old_state, BotStates.JOINING)
        self.assertEqual(bot_joined_meeting_event.new_state, BotStates.JOINED_NOT_RECORDING)
        self.assertIsNone(bot_joined_meeting_event.event_sub_type)
        self.assertEqual(bot_joined_meeting_event.metadata, {})
        self.assertIsNone(bot_joined_meeting_event.requested_bot_action_taken_at)

        # Verify recording_permission_granted_event (Event 3)
        recording_permission_granted_event = bot_events[2]
        self.assertEqual(
            recording_permission_granted_event.event_type,
            BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED,
        )
        self.assertEqual(recording_permission_granted_event.old_state, BotStates.JOINED_NOT_RECORDING)
        self.assertEqual(recording_permission_granted_event.new_state, BotStates.JOINED_RECORDING)
        self.assertIsNone(recording_permission_granted_event.event_sub_type)
        self.assertEqual(recording_permission_granted_event.metadata, {})
        self.assertIsNone(recording_permission_granted_event.requested_bot_action_taken_at)

        # Verify meeting_ended_event (Event 4)
        meeting_ended_event = bot_events[3]
        self.assertEqual(meeting_ended_event.event_type, BotEventTypes.MEETING_ENDED)
        self.assertEqual(meeting_ended_event.old_state, BotStates.JOINED_RECORDING)
        self.assertEqual(meeting_ended_event.new_state, BotStates.POST_PROCESSING)
        self.assertIsNone(meeting_ended_event.event_sub_type)
        self.assertEqual(meeting_ended_event.metadata, {})
        self.assertIsNone(meeting_ended_event.requested_bot_action_taken_at)

        # Verify post_processing_completed_event (Event 5)
        post_processing_completed_event = bot_events[4]
        self.assertEqual(post_processing_completed_event.event_type, BotEventTypes.POST_PROCESSING_COMPLETED)
        self.assertEqual(post_processing_completed_event.old_state, BotStates.POST_PROCESSING)
        self.assertEqual(post_processing_completed_event.new_state, BotStates.ENDED)

        # Verify expected SDK calls
        mock_zoom_sdk_adapter.InitSDK.assert_called_once()
        mock_zoom_sdk_adapter.CreateMeetingService.assert_called_once()
        mock_zoom_sdk_adapter.CreateAuthService.assert_called_once()
        controller.adapter.meeting_service.Join.assert_called_once()

        # Verify audio request was processed
        audio_request.refresh_from_db()
        self.assertEqual(audio_request.state, BotMediaRequestStates.FINISHED)

        # Verify speech request was processed
        speech_request.refresh_from_db()
        self.assertEqual(speech_request.state, BotMediaRequestStates.FINISHED)

        # Verify image request was processed
        image_request.refresh_from_db()
        self.assertEqual(image_request.state, BotMediaRequestStates.FINISHED)

        # Verify that the recording was finished
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)
        self.assertEqual(self.recording.transcription_state, RecordingTranscriptionStates.COMPLETE)
        self.assertEqual(self.recording.transcription_failure_data, None)

        # Verify that the recording has an utterance
        utterance = self.recording.utterances.filter(failure_data__isnull=True).first()
        self.assertEqual(self.recording.utterances.count(), 1)
        self.assertIsNotNone(utterance.transcription)

        # Verify that the recording has an audio chunk
        self.assertEqual(self.recording.audio_chunks.count(), 1)
        self.assertEqual(utterance.audio_chunk, self.recording.audio_chunks.first())

        # Verify chat message was processed
        chat_messages = ChatMessage.objects.filter(bot=self.bot)
        self.assertEqual(chat_messages.count(), 1)
        chat_message = chat_messages.first()
        self.assertEqual(chat_message.text, "Hello bot from Test User!")
        self.assertEqual(chat_message.participant.full_name, "Test User")
        self.assertEqual(chat_message.source_uuid, self.recording.object_id + "-test_chat_message_id_001")
        self.assertEqual(chat_message.to, ChatMessageToOptions.ONLY_BOT)

        # Verify chat message request was processed
        self.chat_message_request.refresh_from_db()
        self.assertEqual(self.chat_message_request.state, BotChatMessageRequestStates.SENT)

        # Verify the bot adapter received the media
        controller.adapter.audio_raw_data_sender.send.assert_has_calls(
            [
                # First call from audio request
                call(
                    mp3_to_pcm(self.test_mp3_bytes, sample_rate=44100),
                    44100,
                    mock_zoom_sdk_adapter.ZoomSDKAudioChannel_Mono,
                ),
                # Second call from text-to-speech
                call(
                    pcm_speech_data,
                    44100,
                    mock_zoom_sdk_adapter.ZoomSDKAudioChannel_Mono,
                ),
            ],
            any_order=True,
        )

        yuv_image, yuv_image_width, yuv_image_height = png_to_yuv420_frame(self.test_png_bytes)
        controller.adapter.video_sender.sendVideoFrame.assert_has_calls(
            [
                call(
                    scale_i420(yuv_image, (yuv_image_width, yuv_image_height), (640, 480)),
                    640,
                    480,
                    0,
                    mock_zoom_sdk_adapter.FrameDataFormat_I420_FULL,
                )
            ],
            any_order=True,
        )

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

        # Verify that the bot has participants
        self.assertEqual(self.bot.participants.count(), 2)
        bot_participant = self.bot.participants.get(user_uuid="bot_persistent_id")
        self.assertEqual(bot_participant.full_name, "Bot User")
        other_participant = self.bot.participants.get(user_uuid="test_persistent_id_123")
        self.assertEqual(other_participant.full_name, "Test User")

        # Verify that the expected participant events were created
        participant_events = ParticipantEvent.objects.filter(participant__bot=self.bot, participant__user_uuid="test_persistent_id_123")
        self.assertEqual(participant_events.count(), 2)
        self.assertEqual(participant_events[0].event_type, ParticipantEventTypes.JOIN)
        self.assertEqual(participant_events[1].event_type, ParticipantEventTypes.LEAVE)

        bot_participant_events = ParticipantEvent.objects.filter(participant__bot=self.bot, participant__user_uuid="bot_persistent_id")
        self.assertEqual(bot_participant_events.count(), 1)
        self.assertEqual(bot_participant_events[0].event_type, ParticipantEventTypes.JOIN)

        # Delete the bot data
        self.bot.delete_data()

        # Verify data was properly deleted
        # Refresh bot from database to get latest state
        self.bot.refresh_from_db()

        # 1. Verify all participants were deleted
        self.assertEqual(self.bot.participants.count(), 0, "Participants were not deleted")

        # 2. Verify all utterances for all recordings were deleted
        for recording in self.bot.recordings.all():
            self.assertEqual(recording.utterances.count(), 0, f"Utterances for recording {recording.id} were not deleted")

        # 3. Verify recording files were deleted (if they existed)
        for recording in self.bot.recordings.all():
            self.assertFalse(recording.file.name, f"Recording file for recording {recording.id} was not deleted")

        # 4. Verify a DATA_DELETED event was created
        self.assertTrue(self.bot.bot_events.filter(event_type=BotEventTypes.DATA_DELETED).exists(), "DATA_DELETED event was not created")

        # 5. Verify that the bot is in the DATA_DELETED state
        self.assertEqual(self.bot.state, BotStates.DATA_DELETED)

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("deepgram.DeepgramClient")
    def test_bot_can_join_meeting_and_record_audio_when_in_voice_agent_configuration(
        self,
        MockDeepgramClient,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        self.bot.settings = {
            "recording_settings": {
                "format": RecordingFormats.MP4,
            }
        }
        self.bot.save()

        # Set up Deepgram mock
        MockDeepgramClient.return_value = create_mock_deepgram()

        # Store uploaded data for verification
        uploaded_data = bytearray()

        # Configure the mock uploader to capture uploaded data
        mock_uploader = create_mock_file_uploader()

        def capture_upload_part(file_path):
            uploaded_data.extend(open(file_path, "rb").read())

        mock_uploader.upload_file.side_effect = capture_upload_part
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Create bot controller
        controller = BotController(self.bot.id)
        controller.pipeline_configuration = PipelineConfiguration.audio_recorder_bot_with_websocket_audio()

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        audio_request = None
        image_request = None
        speech_request = None

        def simulate_join_flow():
            nonlocal audio_request, image_request, speech_request

            adapter = controller.adapter
            # Simulate successful auth
            adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)

            # Simulate connecting
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Simulate successful join
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            time.sleep(2)

            # Simulate audio frame received
            adapter.audio_source.onOneWayAudioRawDataReceivedCallback(
                MockPCMAudioFrame(),
                2,  # Simulated participant ID that's not the bot
            )

            # simulate audio mic initialized
            adapter.virtual_audio_mic_event_passthrough.onMicInitializeCallback(MagicMock())

            # simulate audio mic started
            adapter.virtual_audio_mic_event_passthrough.onMicStartSendCallback()

            # simulate video source initialized
            mock_suggest_cap = MagicMock()
            mock_suggest_cap.width = 640
            mock_suggest_cap.height = 480
            mock_suggest_cap.frame = 30
            adapter.virtual_camera_video_source.onInitializeCallback(MagicMock(), [], mock_suggest_cap)

            # simulate video source started
            adapter.virtual_camera_video_source.onStartSendCallback()

            time.sleep(2)

            # Simulate meeting ended
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_ENDED,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # sleep a bit for the utterance to be saved
            time.sleep(5)

            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(3, simulate_join_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=10)

        # Verify that we received no data
        self.assertEqual(len(uploaded_data), 993, "Uploaded data length is not correct")

        # Additional verification for FileUploader
        mock_uploader.upload_file.assert_called_once()
        self.assertGreater(mock_uploader.upload_file.call_count, 0, "upload_file was never called")
        mock_uploader.wait_for_upload.assert_called_once()
        mock_uploader.delete_file.assert_called_once()

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Assert that the bot is in the ENDED state
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Verify all bot events in sequence
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 5)  # We expect 5 events in total

        # Verify join_requested_event (Event 1)
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)
        self.assertIsNone(join_requested_event.event_sub_type)
        self.assertEqual(join_requested_event.metadata, {})
        self.assertIsNotNone(join_requested_event.requested_bot_action_taken_at)

        # Verify bot_joined_meeting_event (Event 2)
        bot_joined_meeting_event = bot_events[1]
        self.assertEqual(bot_joined_meeting_event.event_type, BotEventTypes.BOT_JOINED_MEETING)
        self.assertEqual(bot_joined_meeting_event.old_state, BotStates.JOINING)
        self.assertEqual(bot_joined_meeting_event.new_state, BotStates.JOINED_NOT_RECORDING)
        self.assertIsNone(bot_joined_meeting_event.event_sub_type)
        self.assertEqual(bot_joined_meeting_event.metadata, {})
        self.assertIsNone(bot_joined_meeting_event.requested_bot_action_taken_at)

        # Verify recording_permission_granted_event (Event 3)
        recording_permission_granted_event = bot_events[2]
        self.assertEqual(
            recording_permission_granted_event.event_type,
            BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED,
        )
        self.assertEqual(recording_permission_granted_event.old_state, BotStates.JOINED_NOT_RECORDING)
        self.assertEqual(recording_permission_granted_event.new_state, BotStates.JOINED_RECORDING)
        self.assertIsNone(recording_permission_granted_event.event_sub_type)
        self.assertEqual(recording_permission_granted_event.metadata, {})
        self.assertIsNone(recording_permission_granted_event.requested_bot_action_taken_at)

        # Verify meeting_ended_event (Event 4)
        meeting_ended_event = bot_events[3]
        self.assertEqual(meeting_ended_event.event_type, BotEventTypes.MEETING_ENDED)
        self.assertEqual(meeting_ended_event.old_state, BotStates.JOINED_RECORDING)
        self.assertEqual(meeting_ended_event.new_state, BotStates.POST_PROCESSING)
        self.assertIsNone(meeting_ended_event.event_sub_type)
        self.assertEqual(meeting_ended_event.metadata, {})
        self.assertIsNone(meeting_ended_event.requested_bot_action_taken_at)

        # Verify post_processing_completed_event (Event 5)
        post_processing_completed_event = bot_events[4]
        self.assertEqual(post_processing_completed_event.event_type, BotEventTypes.POST_PROCESSING_COMPLETED)
        self.assertEqual(post_processing_completed_event.old_state, BotStates.POST_PROCESSING)
        self.assertEqual(post_processing_completed_event.new_state, BotStates.ENDED)

        # Verify expected SDK calls
        mock_zoom_sdk_adapter.InitSDK.assert_called_once()
        mock_zoom_sdk_adapter.CreateMeetingService.assert_called_once()
        mock_zoom_sdk_adapter.CreateAuthService.assert_called_once()
        controller.adapter.meeting_service.Join.assert_called_once()

        # Verify that the recording was finished
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)
        self.assertEqual(self.recording.transcription_state, RecordingTranscriptionStates.COMPLETE)

        # Verify that the recording has an utterance
        utterance = self.recording.utterances.first()
        self.assertEqual(self.recording.utterances.count(), 1)
        self.assertIsNotNone(utterance.transcription)

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    def test_bot_can_handle_failed_zoom_auth(
        self,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        # Configure the mock class to return our mock instance
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_failed_auth_flow():
            # Simulate failed auth
            controller.adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_JWTTOKENWRONG)
            # Clean up connections in thread
            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(2, simulate_failed_auth_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=10)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Check that the bot joined successfully
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 2)
        join_requested_event = bot_events[0]
        could_not_join_event = bot_events[1]

        # Verify join_requested_event properties
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)
        self.assertIsNone(join_requested_event.event_sub_type)
        self.assertEqual(join_requested_event.metadata, {})
        self.assertIsNotNone(join_requested_event.requested_bot_action_taken_at)

        # Verify could_not_join_event properties
        self.assertEqual(could_not_join_event.event_type, BotEventTypes.COULD_NOT_JOIN)
        self.assertEqual(could_not_join_event.old_state, BotStates.JOINING)
        self.assertEqual(could_not_join_event.new_state, BotStates.FATAL_ERROR)
        self.assertEqual(
            could_not_join_event.event_sub_type,
            BotEventSubTypes.COULD_NOT_JOIN_MEETING_ZOOM_AUTHORIZATION_FAILED,
        )
        self.assertEqual(
            could_not_join_event.metadata,
            {"zoom_result_code": str(mock_zoom_sdk_adapter.AUTHRET_JWTTOKENWRONG), "bot_duration_seconds": 30, "credits_consumed": 0.01},
        )
        self.assertIsNone(could_not_join_event.requested_bot_action_taken_at)

        # Verify recording state and transcription state is not started
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.NOT_STARTED)
        self.assertEqual(self.recording.transcription_state, RecordingTranscriptionStates.NOT_STARTED)
        self.assertEqual(self.recording.transcription_failure_data, None)

        # Verify expected SDK calls
        mock_zoom_sdk_adapter.InitSDK.assert_called_once()
        mock_zoom_sdk_adapter.CreateMeetingService.assert_called_once()
        mock_zoom_sdk_adapter.CreateAuthService.assert_called_once()
        controller.adapter.meeting_service.Join.assert_not_called()

        # Additional verification for FileUploader
        # Probably should not be called, but it currently is
        # controller.file_uploader.upload_file.assert_not_called()

        # Cleanup
        # no need to cleanup since we already hit error
        # controller.cleanup() will be called by the bot controller

        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    def test_bot_can_handle_waiting_for_host(
        self,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        # Configure the mock class to return our mock instance
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Create bot controller
        controller = BotController(self.bot.id)
        controller.automatic_leave_configuration = AutomaticLeaveConfiguration(wait_for_host_to_start_meeting_timeout_seconds=1)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_waiting_for_host_flow():
            # Simulate successful auth
            controller.adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)
            # Simulate waiting for host status
            controller.adapter.meeting_service_event.onMeetingStatusChangedCallback(mock_zoom_sdk_adapter.MEETING_STATUS_WAITINGFORHOST, 0)
            # Clean up connections in thread
            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(2, simulate_waiting_for_host_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=10)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Check the bot events
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 2)
        join_requested_event = bot_events[0]
        could_not_join_event = bot_events[1]

        # Verify join_requested_event properties
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)
        self.assertIsNone(join_requested_event.event_sub_type)
        self.assertEqual(join_requested_event.metadata, {})
        self.assertIsNotNone(join_requested_event.requested_bot_action_taken_at)

        # Verify could_not_join_event properties
        self.assertEqual(could_not_join_event.event_type, BotEventTypes.COULD_NOT_JOIN)
        self.assertEqual(could_not_join_event.old_state, BotStates.JOINING)
        self.assertEqual(could_not_join_event.new_state, BotStates.FATAL_ERROR)
        self.assertEqual(
            could_not_join_event.event_sub_type,
            BotEventSubTypes.COULD_NOT_JOIN_MEETING_NOT_STARTED_WAITING_FOR_HOST,
        )
        self.assertEqual(could_not_join_event.metadata, {"bot_duration_seconds": 30, "credits_consumed": 0.01})
        self.assertIsNone(could_not_join_event.requested_bot_action_taken_at)

        # Verify expected SDK calls
        mock_zoom_sdk_adapter.InitSDK.assert_called_once()
        mock_zoom_sdk_adapter.CreateMeetingService.assert_called_once()
        mock_zoom_sdk_adapter.CreateAuthService.assert_called_once()
        controller.adapter.meeting_service.Join.assert_called_once()

        # Cleanup
        # no need to cleanup since we already hit error
        # controller.cleanup() will be called by the bot controller
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    def test_bot_can_handle_unable_to_join_external_meeting(
        self,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        # Configure the mock class to return our mock instance
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_unable_to_join_external_meeting_flow():
            # Simulate successful auth
            controller.adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)
            # Simulate meeting failed status with unable to join external meeting code
            controller.adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_FAILED,
                mock_zoom_sdk_adapter.MeetingFailCode.MEETING_FAIL_UNABLE_TO_JOIN_EXTERNAL_MEETING,
            )
            # Clean up connections in thread
            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(2, simulate_unable_to_join_external_meeting_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=10)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Check the bot events
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 2)
        join_requested_event = bot_events[0]
        could_not_join_event = bot_events[1]

        # Verify join_requested_event properties
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)
        self.assertIsNone(join_requested_event.event_sub_type)
        self.assertEqual(join_requested_event.metadata, {})
        self.assertIsNotNone(join_requested_event.requested_bot_action_taken_at)

        # Verify could_not_join_event properties
        self.assertEqual(could_not_join_event.event_type, BotEventTypes.COULD_NOT_JOIN)
        self.assertEqual(could_not_join_event.old_state, BotStates.JOINING)
        self.assertEqual(could_not_join_event.new_state, BotStates.FATAL_ERROR)
        self.assertEqual(
            could_not_join_event.event_sub_type,
            BotEventSubTypes.COULD_NOT_JOIN_MEETING_UNPUBLISHED_ZOOM_APP,
        )
        self.assertEqual(
            could_not_join_event.metadata,
            {"zoom_result_code": str(mock_zoom_sdk_adapter.MeetingFailCode.MEETING_FAIL_UNABLE_TO_JOIN_EXTERNAL_MEETING), "bot_duration_seconds": 30, "credits_consumed": 0.01},
        )
        self.assertIsNone(could_not_join_event.requested_bot_action_taken_at)

        # Verify expected SDK calls
        mock_zoom_sdk_adapter.InitSDK.assert_called_once()
        mock_zoom_sdk_adapter.CreateMeetingService.assert_called_once()
        mock_zoom_sdk_adapter.CreateAuthService.assert_called_once()
        controller.adapter.meeting_service.Join.assert_called_once()

        # Cleanup
        # no need to cleanup since we already hit error
        # controller.cleanup() will be called by the bot controller
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    def test_bot_can_handle_meeting_failed_blocked_by_admin(
        self,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        # Configure the mock class to return our mock instance
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_meeting_failed_flow():
            # Simulate successful auth
            controller.adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)
            # Simulate meeting failed status with blocked by admin code
            controller.adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_FAILED,
                mock_zoom_sdk_adapter.MeetingFailCode.MEETING_FAIL_BLOCKED_BY_ACCOUNT_ADMIN,
            )
            # Clean up connections in thread
            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(2, simulate_meeting_failed_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=10)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Check the bot events
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 2)
        join_requested_event = bot_events[0]
        could_not_join_event = bot_events[1]

        # Verify join_requested_event properties
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)
        self.assertIsNone(join_requested_event.event_sub_type)
        self.assertEqual(join_requested_event.metadata, {})
        self.assertIsNotNone(join_requested_event.requested_bot_action_taken_at)

        # Verify could_not_join_event properties
        self.assertEqual(could_not_join_event.event_type, BotEventTypes.COULD_NOT_JOIN)
        self.assertEqual(could_not_join_event.old_state, BotStates.JOINING)
        self.assertEqual(could_not_join_event.new_state, BotStates.FATAL_ERROR)
        self.assertEqual(
            could_not_join_event.event_sub_type,
            BotEventSubTypes.COULD_NOT_JOIN_MEETING_ZOOM_MEETING_STATUS_FAILED,
        )
        self.assertEqual(
            could_not_join_event.metadata.get("zoom_result_code"),
            str(mock_zoom_sdk_adapter.MeetingFailCode.MEETING_FAIL_BLOCKED_BY_ACCOUNT_ADMIN),
        )
        self.assertIsNone(could_not_join_event.requested_bot_action_taken_at)

        # Verify expected SDK calls
        mock_zoom_sdk_adapter.InitSDK.assert_called_once()
        mock_zoom_sdk_adapter.CreateMeetingService.assert_called_once()
        mock_zoom_sdk_adapter.CreateAuthService.assert_called_once()
        controller.adapter.meeting_service.Join.assert_called_once()

        # Cleanup
        # no need to cleanup since we already hit error
        # controller.cleanup() will be called by the bot controller
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("deepgram.DeepgramClient")

    # We need run this test last because if the process isn't killed properly some weird behavior ensues
    # where the thread is still running even after the test is over. It's due to the fact that multiple tests
    # are run in a single process.
    # So we put a 'z' in the test name to run it last.
    # This is a temporary hack, but it's ok for now IMO. In production, the process would be killed
    def test_bot_z_handles_rtmp_connection_failure(
        self,
        MockDeepgramClient,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        # Set up Deepgram mock
        MockDeepgramClient.return_value = create_mock_deepgram()

        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Set RTMP URL for the bot
        self.bot.settings = {
            "rtmp_settings": {
                "destination_url": "rtmp://example.com/live/stream",
                "stream_key": "1234",
            }
        }
        self.bot.save()

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_join_flow():
            adapter = controller.adapter
            # Simulate successful auth
            adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)

            # Simulate connecting
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Simulate successful join
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Wait for the video input manager to be set up
            time.sleep(2)

            # Send a bunch of frames to the bot it takes some time to recognize the rtmp failure
            for i in range(5):
                # Simulate video frame received
                adapter.video_input_manager.input_streams[0].renderer_delegate.onRawDataFrameReceivedCallback(MockVideoFrame())

                # Simulate audio frame received
                adapter.audio_source.onOneWayAudioRawDataReceivedCallback(
                    MockPCMAudioFrame(),
                    2,  # Simulated participant ID that's not the bot
                )
                adapter.audio_source.onMixedAudioRawDataReceivedCallback(MockPCMAudioFrame())

                time.sleep(5.0)

            # Error will be triggered because the rtmp url we gave was bad
            # This will trigger the GStreamer pipeline to send a message to the bot
            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(3, simulate_join_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=40)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Assert that the bot is in the FATAL_ERROR state
        self.assertEqual(self.bot.state, BotStates.FATAL_ERROR)

        # Verify bot events in sequence
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 4)  # We expect 4 events in total

        # Verify join_requested_event (Event 1)
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)
        self.assertIsNone(join_requested_event.event_sub_type)
        self.assertEqual(join_requested_event.metadata, {})
        self.assertIsNotNone(join_requested_event.requested_bot_action_taken_at)

        # Verify bot_joined_meeting_event (Event 2)
        bot_joined_meeting_event = bot_events[1]
        self.assertEqual(bot_joined_meeting_event.event_type, BotEventTypes.BOT_JOINED_MEETING)
        self.assertEqual(bot_joined_meeting_event.old_state, BotStates.JOINING)
        self.assertEqual(bot_joined_meeting_event.new_state, BotStates.JOINED_NOT_RECORDING)
        self.assertIsNone(bot_joined_meeting_event.event_sub_type)
        self.assertEqual(bot_joined_meeting_event.metadata, {})
        self.assertIsNone(bot_joined_meeting_event.requested_bot_action_taken_at)

        # Verify recording_permission_granted_event (Event 3)
        recording_permission_granted_event = bot_events[2]
        self.assertEqual(
            recording_permission_granted_event.event_type,
            BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED,
        )
        self.assertEqual(recording_permission_granted_event.old_state, BotStates.JOINED_NOT_RECORDING)
        self.assertEqual(recording_permission_granted_event.new_state, BotStates.JOINED_RECORDING)
        self.assertIsNone(recording_permission_granted_event.event_sub_type)
        self.assertEqual(recording_permission_granted_event.metadata, {})
        self.assertIsNone(recording_permission_granted_event.requested_bot_action_taken_at)

        # Verify fatal_error_event (Event 4)
        fatal_error_event = bot_events[3]
        self.assertEqual(fatal_error_event.event_type, BotEventTypes.FATAL_ERROR)
        self.assertEqual(fatal_error_event.old_state, BotStates.JOINED_RECORDING)
        self.assertEqual(fatal_error_event.new_state, BotStates.FATAL_ERROR)
        self.assertEqual(
            fatal_error_event.event_sub_type,
            BotEventSubTypes.FATAL_ERROR_RTMP_CONNECTION_FAILED,
        )
        self.assertEqual(
            fatal_error_event.metadata.get("rtmp_destination_url"),
            "rtmp://example.com/live/stream/1234",
        )

        # Verify recording state and transcription state is not started
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.FAILED)
        self.assertEqual(self.recording.transcription_state, RecordingTranscriptionStates.COMPLETE)
        self.assertEqual(self.recording.transcription_failure_data, None)

        # Verify that the bot did not incur charges
        credit_transaction = CreditTransaction.objects.filter(bot=self.bot).first()
        self.assertIsNone(credit_transaction, "A credit transaction was created for the bot")

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    def test_bot_can_handle_zoom_sdk_internal_error(
        self,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        # Configure the mock class to return our mock instance
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Configure the auth service to return an error
        mock_zoom_sdk_adapter.CreateAuthService.return_value.SDKAuth.return_value = mock_zoom_sdk_adapter.SDKError.SDKERR_INTERNAL_ERROR

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        # Give the bot some time to process
        bot_thread.join(timeout=10)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Check the bot events
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 2)
        join_requested_event = bot_events[0]
        could_not_join_event = bot_events[1]

        # Verify join_requested_event properties
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)
        self.assertIsNone(join_requested_event.event_sub_type)
        self.assertEqual(join_requested_event.metadata, {})
        self.assertIsNotNone(join_requested_event.requested_bot_action_taken_at)

        # Verify could_not_join_event properties
        self.assertEqual(could_not_join_event.event_type, BotEventTypes.COULD_NOT_JOIN)
        self.assertEqual(could_not_join_event.old_state, BotStates.JOINING)
        self.assertEqual(could_not_join_event.new_state, BotStates.FATAL_ERROR)
        self.assertEqual(
            could_not_join_event.event_sub_type,
            BotEventSubTypes.COULD_NOT_JOIN_MEETING_ZOOM_SDK_INTERNAL_ERROR,
        )
        self.assertEqual(
            could_not_join_event.metadata,
            {"zoom_result_code": str(mock_zoom_sdk_adapter.SDKError.SDKERR_INTERNAL_ERROR), "bot_duration_seconds": 30, "credits_consumed": 0.01},
        )
        self.assertIsNone(could_not_join_event.requested_bot_action_taken_at)

        # Verify expected SDK calls
        mock_zoom_sdk_adapter.InitSDK.assert_called_once()
        mock_zoom_sdk_adapter.CreateMeetingService.assert_called_once()
        mock_zoom_sdk_adapter.CreateAuthService.assert_called_once()
        controller.adapter.meeting_service.Join.assert_not_called()

        # Cleanup
        # no need to cleanup since we already hit error
        # controller.cleanup() will be called by the bot controller
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("deepgram.DeepgramClient")
    def test_bot_leaves_meeting_when_requested(
        self,
        MockDeepgramClient,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        # Set up Deepgram mock
        MockDeepgramClient.return_value = create_mock_deepgram()

        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_join_flow():
            adapter = controller.adapter
            # Simulate successful auth
            adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)

            # Configure GetMeetingStatus to return the correct status
            adapter.meeting_service.GetMeetingStatus.return_value = mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING

            # Simulate connecting
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Update GetMeetingStatus to return in-meeting status
            adapter.meeting_service.GetMeetingStatus.return_value = mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING

            # Simulate successful join
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Wait for the video input manager to be set up
            time.sleep(2)

            # Simulate audio frame received to trigger transcription
            adapter.audio_source.onOneWayAudioRawDataReceivedCallback(
                MockPCMAudioFrame(),
                2,  # Simulated participant ID that's not the bot
            )

            # Give no time for the transcription to be processed
            time.sleep(0.1)

            # Simulate user requesting bot to leave
            BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.LEAVE_REQUESTED, event_sub_type=BotEventSubTypes.LEAVE_REQUESTED_USER_REQUESTED)
            controller.handle_redis_message({"type": "message", "data": json.dumps({"command": "sync"}).encode("utf-8")})

            # Update GetMeetingStatus to return ended status when meeting ends
            adapter.meeting_service.GetMeetingStatus.return_value = mock_zoom_sdk_adapter.MEETING_STATUS_ENDED

            # Simulate meeting ended after leave
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_ENDED,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Clean up connections in thread
            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(2, simulate_join_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=10)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Assert that the bot is in the ENDED state
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Verify bot events in sequence
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 6)  # We expect 6 events in total

        # Verify join_requested_event (Event 1)
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)

        # Verify bot_joined_meeting_event (Event 2)
        bot_joined_meeting_event = bot_events[1]
        self.assertEqual(bot_joined_meeting_event.event_type, BotEventTypes.BOT_JOINED_MEETING)
        self.assertEqual(bot_joined_meeting_event.old_state, BotStates.JOINING)
        self.assertEqual(bot_joined_meeting_event.new_state, BotStates.JOINED_NOT_RECORDING)

        # Verify recording_permission_granted_event (Event 3)
        recording_permission_granted_event = bot_events[2]
        self.assertEqual(
            recording_permission_granted_event.event_type,
            BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED,
        )
        self.assertEqual(recording_permission_granted_event.old_state, BotStates.JOINED_NOT_RECORDING)
        self.assertEqual(recording_permission_granted_event.new_state, BotStates.JOINED_RECORDING)

        # Verify leave_requested_event (Event 4)
        leave_requested_event = bot_events[3]
        self.assertEqual(leave_requested_event.event_type, BotEventTypes.LEAVE_REQUESTED)
        self.assertEqual(leave_requested_event.old_state, BotStates.JOINED_RECORDING)
        self.assertEqual(leave_requested_event.new_state, BotStates.LEAVING)
        self.assertEqual(leave_requested_event.metadata, {})  # No metadata for user-requested leave
        self.assertEqual(
            leave_requested_event.event_sub_type,
            BotEventSubTypes.LEAVE_REQUESTED_USER_REQUESTED,
        )

        # Verify bot_left_meeting_event (Event 5)
        bot_left_meeting_event = bot_events[4]
        self.assertEqual(bot_left_meeting_event.event_type, BotEventTypes.BOT_LEFT_MEETING)
        self.assertEqual(bot_left_meeting_event.old_state, BotStates.LEAVING)
        self.assertEqual(bot_left_meeting_event.new_state, BotStates.POST_PROCESSING)

        # Verify post_processing_completed_event (Event 6)
        post_processing_completed_event = bot_events[5]
        self.assertEqual(post_processing_completed_event.event_type, BotEventTypes.POST_PROCESSING_COMPLETED)
        self.assertEqual(post_processing_completed_event.old_state, BotStates.POST_PROCESSING)
        self.assertEqual(post_processing_completed_event.new_state, BotStates.ENDED)

        # Verify that the adapter's leave method was called with the correct reason
        controller.adapter.meeting_service.Leave.assert_called_once_with(mock_zoom_sdk_adapter.LEAVE_MEETING)

        # Verify that the recording has an utterance
        self.recording.refresh_from_db()
        utterances = self.recording.utterances.all()
        self.assertEqual(utterances.count(), 1)
        utterance = utterances.first()
        self.assertEqual(utterance.transcription.get("transcript"), "This is a test transcript")
        self.assertEqual(utterance.participant.uuid, "2")  # The simulated participant ID
        self.assertEqual(utterance.participant.full_name, "Test User")

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("deepgram.DeepgramClient")
    def test_bot_handles_deepgram_credential_failure(
        self,
        MockDeepgramClient,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        import deepgram

        # Set up Deepgram mock to raise an error for invalid credentials
        mock_deepgram = create_mock_deepgram()
        # Simulate invalid credentials error from Deepgram
        deepgram_api_error = deepgram.DeepgramApiError(message="Invalid authentication", status="401", original_error='{"err_code": "INVALID_AUTH", "err_msg": "Invalid authentication"}')
        mock_deepgram.listen.rest.v.return_value.transcribe_file.side_effect = deepgram_api_error
        MockDeepgramClient.return_value = mock_deepgram

        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_join_flow():
            adapter = controller.adapter
            # Simulate successful auth
            adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)

            # Simulate connecting
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Simulate successful join
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Wait for the video input manager to be set up
            time.sleep(2)

            # Simulate audio frame received to trigger transcription
            adapter.audio_source.onOneWayAudioRawDataReceivedCallback(
                MockPCMAudioFrame(),
                2,  # Simulated participant ID that's not the bot
            )

            # Give time for the transcription to fail
            time.sleep(3)

            # Simulate meeting ended
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_ENDED,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # sleep a bit for the utterance to be saved
            time.sleep(5)

            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(3, simulate_join_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=10)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Assert that the bot is in the ENDED state
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Verify all bot events in sequence
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 5)  # We expect 5 events in total

        # Verify join_requested_event (Event 1)
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)

        # Verify bot_joined_meeting_event (Event 2)
        bot_joined_meeting_event = bot_events[1]
        self.assertEqual(bot_joined_meeting_event.event_type, BotEventTypes.BOT_JOINED_MEETING)

        # Verify recording_permission_granted_event (Event 3)
        recording_permission_granted_event = bot_events[2]
        self.assertEqual(
            recording_permission_granted_event.event_type,
            BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED,
        )

        # Verify meeting_ended_event (Event 4)
        meeting_ended_event = bot_events[3]
        self.assertEqual(meeting_ended_event.event_type, BotEventTypes.MEETING_ENDED)

        # Verify post_processing_completed_event (Event 5)
        post_processing_completed_event = bot_events[4]
        self.assertEqual(post_processing_completed_event.event_type, BotEventTypes.POST_PROCESSING_COMPLETED)

        # Verify post processing metadata contains transcription failure info
        self.assertEqual(post_processing_completed_event.metadata.get("transcription_errors"), [TranscriptionFailureReasons.CREDENTIALS_INVALID])

        # Verify that the recording was finished successfully
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)
        self.assertEqual(self.recording.transcription_state, RecordingTranscriptionStates.FAILED)

        # Check the transcription failure data
        self.assertIsNotNone(self.recording.transcription_failure_data)
        self.assertEqual(self.recording.transcription_failure_data.get("failure_reasons"), [TranscriptionFailureReasons.CREDENTIALS_INVALID])

        # Verify that the utterance has the expected failure data
        utterances = self.recording.utterances.all()
        self.assertEqual(utterances.count(), 1)
        utterance = utterances.first()
        self.assertIsNone(utterance.transcription)
        self.assertIsNotNone(utterance.failure_data)
        self.assertEqual(utterance.failure_data.get("reason"), TranscriptionFailureReasons.CREDENTIALS_INVALID)

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("bots.tasks.process_utterance_task.process_utterance")
    def test_bot_handles_transcription_job_never_runs(
        self,
        mock_process_utterance,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        # Mock the process_utterance task to never run
        mock_process_utterance.delay.return_value = None

        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Create bot controller
        controller = BotController(self.bot.id)
        controller.UTTERANCE_TERMINATION_WAIT_TIME_SECONDS = 1

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_join_flow():
            adapter = controller.adapter
            # Simulate successful auth
            adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)

            # Simulate connecting
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Simulate successful join
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Wait for the video input manager to be set up
            time.sleep(2)

            # Simulate audio frame received
            adapter.audio_source.onOneWayAudioRawDataReceivedCallback(
                MockPCMAudioFrame(),
                2,  # Simulated participant ID that's not the bot
            )

            # Give time for utterance to be saved
            time.sleep(3)

            # Simulate meeting ended
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_ENDED,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # sleep a bit for the utterance to be saved and post-processing to complete
            time.sleep(5)

            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(3, simulate_join_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=15)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Assert that the bot is in the ENDED state
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Verify all bot events in sequence
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 5)  # We expect 5 events in total

        # Verify join_requested_event (Event 1)
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)

        # Verify bot_joined_meeting_event (Event 2)
        bot_joined_meeting_event = bot_events[1]
        self.assertEqual(bot_joined_meeting_event.event_type, BotEventTypes.BOT_JOINED_MEETING)

        # Verify recording_permission_granted_event (Event 3)
        recording_permission_granted_event = bot_events[2]
        self.assertEqual(
            recording_permission_granted_event.event_type,
            BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED,
        )

        # Verify meeting_ended_event (Event 4)
        meeting_ended_event = bot_events[3]
        self.assertEqual(meeting_ended_event.event_type, BotEventTypes.MEETING_ENDED)

        # Verify post_processing_completed_event (Event 5)
        post_processing_completed_event = bot_events[4]
        self.assertEqual(post_processing_completed_event.event_type, BotEventTypes.POST_PROCESSING_COMPLETED)

        # Verify post processing metadata contains transcription failure info
        self.assertIn("transcription_errors", post_processing_completed_event.metadata)
        self.assertEqual(post_processing_completed_event.metadata.get("transcription_errors"), [TranscriptionFailureReasons.UTTERANCES_STILL_IN_PROGRESS_WHEN_RECORDING_TERMINATED])

        # Verify that the recording was finished successfully
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)
        self.assertEqual(self.recording.transcription_state, RecordingTranscriptionStates.FAILED)

        # Check the transcription failure data
        self.assertIsNotNone(self.recording.transcription_failure_data)
        self.assertEqual(self.recording.transcription_failure_data.get("failure_reasons"), [TranscriptionFailureReasons.UTTERANCES_STILL_IN_PROGRESS_WHEN_RECORDING_TERMINATED])

        # Verify that the utterance was created but never processed
        utterances = self.recording.utterances.all()
        self.assertEqual(utterances.count(), 1)
        utterance = utterances.first()
        self.assertIsNone(utterance.transcription)
        self.assertIsNone(utterance.failure_data)  # No failure data since the task never ran

        # Verify the process_utterance task was called but never executed
        mock_process_utterance.delay.assert_called_once()

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("deepgram.DeepgramClient")
    def test_bot_can_join_meeting_and_record_audio_in_mp3_format(
        self,
        MockDeepgramClient,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        self.bot.settings = {
            "recording_settings": {
                "format": RecordingFormats.MP3,
            }
        }
        self.bot.save()

        # Set up Deepgram mock
        MockDeepgramClient.return_value = create_mock_deepgram()

        # Store uploaded data for verification
        uploaded_data = bytearray()

        # Configure the mock uploader to capture uploaded data
        mock_uploader = create_mock_file_uploader()

        def capture_upload_part(file_path):
            with open(file_path, "rb") as f:
                uploaded_data.extend(f.read())

        mock_uploader.upload_file.side_effect = capture_upload_part
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_join_flow():
            adapter = controller.adapter
            # Simulate successful auth
            adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)

            # Simulate connecting
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Simulate successful join
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Wait for the gstreamer pipeline to be set up
            time.sleep(2)

            # Simulate audio frame received
            adapter.audio_source.onOneWayAudioRawDataReceivedCallback(
                MockPCMAudioFrame(),
                2,  # Simulated participant ID that's not the bot
            )
            adapter.audio_source.onMixedAudioRawDataReceivedCallback(MockPCMAudioFrame())

            # simulate audio mic initialized
            adapter.virtual_audio_mic_event_passthrough.onMicInitializeCallback(MagicMock())

            # simulate audio mic started
            adapter.virtual_audio_mic_event_passthrough.onMicStartSendCallback()

            # sleep a bit for the audio to be processed
            time.sleep(3)

            # Simulate meeting ended
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_ENDED,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(3, simulate_join_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=11)

        # Verify that we received some data
        self.assertGreater(len(uploaded_data), 100, "Uploaded data length is not correct")

        # Check for FLAC file signature
        flac_signature_found = b"fLaC" in uploaded_data[:1000]
        self.assertTrue(flac_signature_found, "FLAC signature not found in uploaded data")

        # Additional verification for FileUploader
        mock_uploader.upload_file.assert_called()
        self.assertGreater(mock_uploader.upload_file.call_count, 0, "upload_file was never called")
        mock_uploader.wait_for_upload.assert_called_once()
        mock_uploader.delete_file.assert_called_once()

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Assert that the bot is in the ENDED state
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Verify all bot events in sequence
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 5)  # We expect 5 events in total

        # Verify join_requested_event (Event 1)
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)

        # Verify bot_joined_meeting_event (Event 2)
        bot_joined_meeting_event = bot_events[1]
        self.assertEqual(bot_joined_meeting_event.event_type, BotEventTypes.BOT_JOINED_MEETING)

        # Verify recording_permission_granted_event (Event 3)
        recording_permission_granted_event = bot_events[2]
        self.assertEqual(
            recording_permission_granted_event.event_type,
            BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED,
        )

        # Verify meeting_ended_event (Event 4)
        meeting_ended_event = bot_events[3]
        self.assertEqual(meeting_ended_event.event_type, BotEventTypes.MEETING_ENDED)

        # Verify post_processing_completed_event (Event 5)
        post_processing_completed_event = bot_events[4]
        self.assertEqual(post_processing_completed_event.event_type, BotEventTypes.POST_PROCESSING_COMPLETED)

        # Verify expected SDK calls
        mock_zoom_sdk_adapter.InitSDK.assert_called_once()
        mock_zoom_sdk_adapter.CreateMeetingService.assert_called_once()
        mock_zoom_sdk_adapter.CreateAuthService.assert_called_once()
        controller.adapter.meeting_service.Join.assert_called_once()

        # Verify that the recording was finished
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)
        self.assertEqual(self.recording.transcription_state, RecordingTranscriptionStates.COMPLETE)

        # Verify that the recording has an utterance
        utterance = self.recording.utterances.filter(failure_data__isnull=True).first()
        self.assertEqual(self.recording.utterances.count(), 1)
        self.assertIsNotNone(utterance.transcription)

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    def test_scheduled_bot_transitions_from_staged_to_joining_at_join_time(
        self,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        from datetime import timedelta

        from django.utils import timezone as django_timezone

        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Set the bot to SCHEDULED state and set a join_at time in the past
        # This way we can test that the bot transitions when the time condition is met
        join_time = django_timezone.now() + timedelta(seconds=5)  # 5 seconds from now
        self.bot.state = BotStates.SCHEDULED
        self.bot.join_at = join_time
        self.bot.save()

        # Clear the old joined event added by the setup code
        self.bot.bot_events.all().delete()

        # Transition the bot to STAGED state (simulating what the scheduler task would do)
        BotEventManager.create_event(bot=self.bot, event_type=BotEventTypes.STAGED, event_metadata={"join_at": join_time.isoformat()})

        # Verify bot is in STAGED state
        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.STAGED)

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def wait_for_transition():
            # Wait for the join time to pass and for the bot to process the time change
            time.sleep(8)  # Wait longer than the join_time (5 seconds) plus processing time

            # Refresh bot and check if it transitioned to JOINING
            self.bot.refresh_from_db()
            # Bot should now be in JOINING state since join time has passed
            self.assertEqual(self.bot.state, BotStates.JOINING)

            # Clean up - we don't need to test the actual joining process, just the transition
            controller.cleanup()

            # Clean up connections in thread
            connection.close()

        # Run simulation after a short delay to let the bot controller start
        threading.Timer(2, wait_for_transition).start()

        # Give the bot some time to process
        bot_thread.join(timeout=15)

        # Refresh the bot from the database one final time
        self.bot.refresh_from_db()

        # Verify that the bot is now in JOINING state
        self.assertEqual(self.bot.state, BotStates.JOINING)

        # Verify the events were created correctly
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 2)  # STAGED and JOIN_REQUESTED events

        # Verify STAGED event
        staged_event = bot_events[0]
        self.assertEqual(staged_event.event_type, BotEventTypes.STAGED)
        self.assertEqual(staged_event.old_state, BotStates.SCHEDULED)
        self.assertEqual(staged_event.new_state, BotStates.STAGED)
        self.assertEqual(staged_event.metadata.get("join_at"), join_time.isoformat())

        # Verify JOIN_REQUESTED event
        join_requested_event = bot_events[1]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.STAGED)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)
        # Verify it was triggered by the scheduler
        from bots.bots_api_utils import BotCreationSource

        self.assertEqual(join_requested_event.metadata.get("source"), BotCreationSource.SCHEDULER)

        # Cleanup
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    def test_bot_can_handle_stuck_in_connecting_state(
        self,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        # Configure the mock class to return our mock instance
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_stuck_in_connecting_flow():
            # Set a shorter timeout for testing (2 seconds instead of 60)
            controller.adapter.stuck_in_connecting_state_timeout = 2

            # Simulate successful auth
            controller.adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)

            # Configure GetMeetingStatus to return connecting status permanently
            controller.adapter.meeting_service.GetMeetingStatus.return_value = mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING

            # Simulate transition to connecting state
            controller.adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # The bot should get stuck here - we don't send any more status changes
            # The adapter should timeout after its configured timeout period (2 seconds)

            # Clean up connections in thread
            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(2, simulate_stuck_in_connecting_flow).start()

        # Give the bot time to process and timeout (2 second timeout + processing time)
        bot_thread.join(timeout=10)

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Check the bot events
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 2)
        join_requested_event = bot_events[0]
        could_not_join_event = bot_events[1]

        # Verify join_requested_event properties
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)
        self.assertIsNone(join_requested_event.event_sub_type)
        self.assertEqual(join_requested_event.metadata, {})
        self.assertIsNotNone(join_requested_event.requested_bot_action_taken_at)

        # Verify could_not_join_event properties
        self.assertEqual(could_not_join_event.event_type, BotEventTypes.COULD_NOT_JOIN)
        self.assertEqual(could_not_join_event.old_state, BotStates.JOINING)
        self.assertEqual(could_not_join_event.new_state, BotStates.FATAL_ERROR)
        self.assertEqual(
            could_not_join_event.event_sub_type,
            BotEventSubTypes.COULD_NOT_JOIN_UNABLE_TO_CONNECT_TO_MEETING,
        )
        self.assertEqual(could_not_join_event.metadata, {"bot_duration_seconds": 30, "credits_consumed": 0.01})
        self.assertIsNone(could_not_join_event.requested_bot_action_taken_at)

        # Verify recording state and transcription state is not started
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.NOT_STARTED)
        self.assertEqual(self.recording.transcription_state, RecordingTranscriptionStates.NOT_STARTED)
        self.assertEqual(self.recording.transcription_failure_data, None)

        # Verify expected SDK calls
        mock_zoom_sdk_adapter.InitSDK.assert_called_once()
        mock_zoom_sdk_adapter.CreateMeetingService.assert_called_once()
        mock_zoom_sdk_adapter.CreateAuthService.assert_called_once()
        controller.adapter.meeting_service.Join.assert_called_once()

        # Cleanup
        # no need to cleanup since we already hit error
        # controller.cleanup() will be called by the bot controller
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch("bots.external_callback_utils.requests.post")
    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    def test_bot_uses_zoom_tokens_from_callback(
        self,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
        mock_requests_post,
    ):
        # Configure the mock class to return our mock instance
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Setup bot with callback url
        self.bot.settings = {"callback_settings": {"zoom_tokens_url": "https://example.com/zoom-tokens"}}
        self.bot.save()

        # Mock the requests.post call
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "zak_token": "fake_zak_token",
            "join_token": "fake_join_token",
            "app_privilege_token": "fake_app_privilege_token",
        }
        mock_requests_post.return_value = mock_response

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_auth_flow():
            # Allow some time for the bot to initialize
            time.sleep(2)

            # The adapter should be created by now
            if not hasattr(controller, "adapter") or not controller.adapter:
                connection.close()
                return  # fail silently and let the main thread assertions fail

            # Simulate successful auth, which will then trigger the Join call
            controller.adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)

            # Clean up connections in thread
            connection.close()

        # Run auth flow simulation after a short delay
        threading.Timer(1, simulate_auth_flow).start()

        # Give the bot some time to process. It will be in the main loop.
        time.sleep(4)

        # Verify requests.post was called
        mock_requests_post.assert_called_once_with(
            "https://example.com/zoom-tokens",
            headers=ANY,
            json=ANY,
            timeout=30,
        )

        # Verify that meeting_service.Join was called with the correct tokens
        controller.adapter.meeting_service.Join.assert_called_once()
        join_call_args = controller.adapter.meeting_service.Join.call_args
        join_param = join_call_args.args[0]
        self.assertEqual(join_param.param.userZAK, "fake_zak_token")
        self.assertEqual(join_param.param.join_token, "fake_join_token")
        self.assertEqual(join_param.param.app_privilege_token, "fake_app_privilege_token")

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("deepgram.DeepgramClient")
    def test_bot_can_join_meeting_with_no_recording_format(
        self,
        MockDeepgramClient,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        # Set recording format to "none"
        self.bot.settings = {
            "recording_settings": {
                "format": RecordingFormats.NONE,
            }
        }
        self.bot.save()

        # Set up Deepgram mock
        MockDeepgramClient.return_value = create_mock_deepgram()

        # Configure the mock uploader
        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_join_flow():
            adapter = controller.adapter
            # Simulate successful auth
            adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)

            # Simulate connecting
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Simulate successful join
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Wait for setup
            time.sleep(2)

            # Simulate audio frame received
            adapter.audio_source.onOneWayAudioRawDataReceivedCallback(
                MockPCMAudioFrame(),
                2,  # Simulated participant ID that's not the bot
            )

            # simulate audio mic initialized
            adapter.virtual_audio_mic_event_passthrough.onMicInitializeCallback(MagicMock())

            # simulate audio mic started
            adapter.virtual_audio_mic_event_passthrough.onMicStartSendCallback()

            # sleep a bit for the audio to be processed
            time.sleep(3)

            # Simulate meeting ended
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_ENDED,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(3, simulate_join_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=11)

        # Verify that NO file was uploaded (recording format is "none")
        mock_uploader.upload_file.assert_not_called()
        mock_uploader.wait_for_upload.assert_not_called()
        mock_uploader.delete_file.assert_not_called()

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Assert that the bot is in the ENDED state
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Verify all bot events in sequence
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 5)  # We expect 5 events in total

        # Verify join_requested_event (Event 1)
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)

        # Verify bot_joined_meeting_event (Event 2)
        bot_joined_meeting_event = bot_events[1]
        self.assertEqual(bot_joined_meeting_event.event_type, BotEventTypes.BOT_JOINED_MEETING)

        # Verify recording_permission_granted_event (Event 3)
        recording_permission_granted_event = bot_events[2]
        self.assertEqual(
            recording_permission_granted_event.event_type,
            BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED,
        )

        # Verify meeting_ended_event (Event 4)
        meeting_ended_event = bot_events[3]
        self.assertEqual(meeting_ended_event.event_type, BotEventTypes.MEETING_ENDED)

        # Verify post_processing_completed_event (Event 5)
        post_processing_completed_event = bot_events[4]
        self.assertEqual(post_processing_completed_event.event_type, BotEventTypes.POST_PROCESSING_COMPLETED)

        # Verify expected SDK calls
        mock_zoom_sdk_adapter.InitSDK.assert_called_once()
        mock_zoom_sdk_adapter.CreateMeetingService.assert_called_once()
        mock_zoom_sdk_adapter.CreateAuthService.assert_called_once()
        controller.adapter.meeting_service.Join.assert_called_once()

        # Verify that the recording was finished even with no file upload
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)
        self.assertEqual(self.recording.transcription_state, RecordingTranscriptionStates.COMPLETE)

        # Verify that the recording has an utterance (transcription still works)
        utterance = self.recording.utterances.filter(failure_data__isnull=True).first()
        self.assertEqual(self.recording.utterances.count(), 1)
        self.assertIsNotNone(utterance.transcription)

        # Verify that the recording has an audio chunk
        self.assertEqual(self.recording.audio_chunks.count(), 1)
        self.assertEqual(utterance.audio_chunk, self.recording.audio_chunks.first())

        # Verify that no recording file was created/saved
        self.assertFalse(self.recording.file.name, "Recording file should not exist when format is 'none'")

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("deepgram.DeepgramClient")
    @patch("google.cloud.texttospeech.TextToSpeechClient")
    @patch("django.db.models.fields.files.FieldFile.delete", autospec=True)
    def test_pause_recording_prevents_chat_messages_and_utterances_from_being_saved(
        self,
        mock_delete_file_field,
        MockTextToSpeechClient,
        MockDeepgramClient,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        mock_delete_file_field.side_effect = mock_file_field_delete_sets_name_to_none

        self.bot.settings = {
            "recording_settings": {
                "format": RecordingFormats.MP4,
            }
        }
        self.bot.save()

        # Set up Google TTS mock
        mock_tts_client = MagicMock()
        mock_tts_response = MagicMock()

        # Create fake PCM audio data (1 second of 44.1kHz audio)
        # WAV header (44 bytes) + PCM data
        wav_header = (
            b"RIFF"  # ChunkID (4 bytes)
            b"\x24\x00\x00\x00"  # ChunkSize (4 bytes)
            b"WAVE"  # Format (4 bytes)
            b"fmt "  # Subchunk1ID (4 bytes)
            b"\x10\x00\x00\x00"  # Subchunk1Size (4 bytes)
            b"\x01\x00"  # AudioFormat (2 bytes)
            b"\x01\x00"  # NumChannels (2 bytes)
            b"\x44\xac\x00\x00"  # SampleRate (4 bytes)
            b"\x88\x58\x01\x00"  # ByteRate (4 bytes)
            b"\x02\x00"  # BlockAlign (2 bytes)
            b"\x10\x00"  # BitsPerSample (2 bytes)
            b"data"  # Subchunk2ID (4 bytes)
            b"\x50\x00\x00\x00"  # Subchunk2Size (4 bytes) - size of audio data
        )
        pcm_speech_data = b"\x00\x00" * (40)  # small period of silence at 44.1kHz
        mock_tts_response.audio_content = wav_header + pcm_speech_data

        # Configure the mock client to return our mock response
        mock_tts_client.synthesize_speech.return_value = mock_tts_response
        MockTextToSpeechClient.from_service_account_info.return_value = mock_tts_client

        # Set up Deepgram mock
        MockDeepgramClient.return_value = create_mock_deepgram()

        # Store uploaded data for verification
        uploaded_data = bytearray()

        # Configure the mock uploader to capture uploaded data
        mock_uploader = create_mock_file_uploader()

        def capture_upload_part(file_path):
            uploaded_data.extend(open(file_path, "rb").read())

        mock_uploader.upload_file.side_effect = capture_upload_part
        MockFileUploader.return_value = mock_uploader

        # Mock the JWT token generation
        mock_jwt.encode.return_value = "fake_jwt_token"

        # Create bot controller
        controller = BotController(self.bot.id)

        # Run the bot in a separate thread since it has an event loop
        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        audio_request = None
        image_request = None
        speech_request = None
        self.chat_message_request = None

        # Create a mock chat message info object
        mock_chat_msg_info = MagicMock()
        mock_chat_msg_info.GetContent.return_value = "Hello bot from Test User!"
        mock_chat_msg_info.GetSenderUserId.return_value = 2  # Test User's ID
        mock_chat_msg_info.GetTimeStamp.return_value = time.time()
        mock_chat_msg_info.GetMessageID.return_value = "test_chat_message_id_001"
        mock_chat_msg_info.IsChatToAllPanelist.return_value = False
        mock_chat_msg_info.IsChatToAll.return_value = False
        mock_chat_msg_info.IsChatToWaitingroom.return_value = False
        mock_chat_msg_info.IsComment.return_value = False
        mock_chat_msg_info.IsThread.return_value = False
        mock_chat_msg_info.GetThreadID.return_value = ""

        mock_chat_msg_info_2 = MagicMock()
        mock_chat_msg_info_2.GetContent.return_value = "Hello bot from Test User! Sent while resumed"
        mock_chat_msg_info_2.GetSenderUserId.return_value = 2  # Test User's ID
        mock_chat_msg_info_2.GetTimeStamp.return_value = time.time()
        mock_chat_msg_info_2.GetMessageID.return_value = "test_chat_message_id_002"
        mock_chat_msg_info_2.IsChatToAllPanelist.return_value = False
        mock_chat_msg_info_2.IsChatToAll.return_value = False
        mock_chat_msg_info_2.IsChatToWaitingroom.return_value = False
        mock_chat_msg_info_2.IsComment.return_value = False
        mock_chat_msg_info_2.IsThread.return_value = False
        mock_chat_msg_info_2.GetThreadID.return_value = ""

        def simulate_join_flow():
            nonlocal audio_request, image_request, speech_request

            adapter = controller.adapter
            # Simulate successful auth
            adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)

            # Simulate connecting
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Simulate successful join
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Wait for the video input manager to be set up
            time.sleep(2)

            # Simulate pause recording
            send_sync_command(self.bot, "pause_recording")

            # Simulate video frame received
            adapter.video_input_manager.input_streams[0].renderer_delegate.onRawDataFrameReceivedCallback(MockVideoFrame())

            # Simulate audio frame received
            adapter.audio_source.onOneWayAudioRawDataReceivedCallback(
                MockPCMAudioFrame(),
                2,  # Simulated participant ID that's not the bot
            )
            adapter.audio_source.onMixedAudioRawDataReceivedCallback(MockPCMAudioFrame())

            # simulate audio mic initialized
            adapter.virtual_audio_mic_event_passthrough.onMicInitializeCallback(MagicMock())

            # simulate audio mic started
            adapter.virtual_audio_mic_event_passthrough.onMicStartSendCallback()

            time.sleep(2)

            # Simulate chat message received
            adapter.on_chat_msg_notification_callback(mock_chat_msg_info, mock_chat_msg_info.GetContent())

            # Simulate resume recording
            send_sync_command(self.bot, "resume_recording")

            time.sleep(2)

            # Simulate chat message received
            adapter.on_chat_msg_notification_callback(mock_chat_msg_info_2, mock_chat_msg_info_2.GetContent())

            # Simulate user leaving
            adapter.on_user_left_callback([2], [])

            # Simulate meeting ended
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_ENDED,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            connection.close()

        # Run join flow simulation after a short delay
        threading.Timer(3, simulate_join_flow).start()

        # Give the bot some time to process
        bot_thread.join(timeout=11)

        # Verify that we received some data
        self.assertGreater(len(uploaded_data), 100, "Uploaded data length is not correct")

        # Check for MP4 file signature (starts with 'ftyp')
        mp4_signature_found = b"ftyp" in uploaded_data[:1000]
        self.assertTrue(mp4_signature_found, "MP4 file signature not found in uploaded data")

        # Additional verification for FileUploader
        mock_uploader.upload_file.assert_called_once()
        self.assertGreater(mock_uploader.upload_file.call_count, 0, "upload_file was never called")
        mock_uploader.wait_for_upload.assert_called_once()
        mock_uploader.delete_file.assert_called_once()

        # Refresh the bot from the database
        self.bot.refresh_from_db()

        # Assert that the heartbeat timestamp was set
        self.assertIsNotNone(self.bot.first_heartbeat_timestamp)
        self.assertIsNotNone(self.bot.last_heartbeat_timestamp)

        # Assert that the bot is in the ENDED state
        self.assertEqual(self.bot.state, BotStates.ENDED)

        # Verify all bot events in sequence
        bot_events = self.bot.bot_events.all()
        self.assertEqual(len(bot_events), 7)  # We expect 7 events in total

        # Verify join_requested_event (Event 1)
        join_requested_event = bot_events[0]
        self.assertEqual(join_requested_event.event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(join_requested_event.old_state, BotStates.READY)
        self.assertEqual(join_requested_event.new_state, BotStates.JOINING)
        self.assertIsNone(join_requested_event.event_sub_type)
        self.assertEqual(join_requested_event.metadata, {})
        self.assertIsNotNone(join_requested_event.requested_bot_action_taken_at)

        # Verify bot_joined_meeting_event (Event 2)
        bot_joined_meeting_event = bot_events[1]
        self.assertEqual(bot_joined_meeting_event.event_type, BotEventTypes.BOT_JOINED_MEETING)
        self.assertEqual(bot_joined_meeting_event.old_state, BotStates.JOINING)
        self.assertEqual(bot_joined_meeting_event.new_state, BotStates.JOINED_NOT_RECORDING)
        self.assertIsNone(bot_joined_meeting_event.event_sub_type)
        self.assertEqual(bot_joined_meeting_event.metadata, {})
        self.assertIsNone(bot_joined_meeting_event.requested_bot_action_taken_at)

        # Verify recording_permission_granted_event (Event 3)
        recording_permission_granted_event = bot_events[2]
        self.assertEqual(
            recording_permission_granted_event.event_type,
            BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED,
        )
        self.assertEqual(recording_permission_granted_event.old_state, BotStates.JOINED_NOT_RECORDING)
        self.assertEqual(recording_permission_granted_event.new_state, BotStates.JOINED_RECORDING)
        self.assertIsNone(recording_permission_granted_event.event_sub_type)
        self.assertEqual(recording_permission_granted_event.metadata, {})
        self.assertIsNone(recording_permission_granted_event.requested_bot_action_taken_at)

        # Verify recording_paused_event (Event 4)
        recording_paused_event = bot_events[3]
        self.assertEqual(recording_paused_event.event_type, BotEventTypes.RECORDING_PAUSED)
        self.assertEqual(recording_paused_event.old_state, BotStates.JOINED_RECORDING)
        self.assertEqual(recording_paused_event.new_state, BotStates.JOINED_RECORDING_PAUSED)
        self.assertIsNone(recording_paused_event.event_sub_type)
        self.assertEqual(recording_paused_event.metadata, {})
        self.assertIsNone(recording_paused_event.requested_bot_action_taken_at)

        # Verify recording_resumed_event (Event 5)
        recording_resumed_event = bot_events[4]
        self.assertEqual(recording_resumed_event.event_type, BotEventTypes.RECORDING_RESUMED)
        self.assertEqual(recording_resumed_event.old_state, BotStates.JOINED_RECORDING_PAUSED)
        self.assertEqual(recording_resumed_event.new_state, BotStates.JOINED_RECORDING)
        self.assertIsNone(recording_resumed_event.event_sub_type)
        self.assertEqual(recording_resumed_event.metadata, {})
        self.assertIsNone(recording_resumed_event.requested_bot_action_taken_at)

        # Verify meeting_ended_event (Event 6)
        meeting_ended_event = bot_events[5]
        self.assertEqual(meeting_ended_event.event_type, BotEventTypes.MEETING_ENDED)
        self.assertEqual(meeting_ended_event.old_state, BotStates.JOINED_RECORDING)
        self.assertEqual(meeting_ended_event.new_state, BotStates.POST_PROCESSING)
        self.assertIsNone(meeting_ended_event.event_sub_type)
        self.assertEqual(meeting_ended_event.metadata, {})
        self.assertIsNone(meeting_ended_event.requested_bot_action_taken_at)

        # Verify post_processing_completed_event (Event 7)
        post_processing_completed_event = bot_events[6]
        self.assertEqual(post_processing_completed_event.event_type, BotEventTypes.POST_PROCESSING_COMPLETED)
        self.assertEqual(post_processing_completed_event.old_state, BotStates.POST_PROCESSING)
        self.assertEqual(post_processing_completed_event.new_state, BotStates.ENDED)

        # Verify expected SDK calls
        mock_zoom_sdk_adapter.InitSDK.assert_called_once()
        mock_zoom_sdk_adapter.CreateMeetingService.assert_called_once()
        mock_zoom_sdk_adapter.CreateAuthService.assert_called_once()
        controller.adapter.meeting_service.Join.assert_called_once()

        # Verify that the recording was finished
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.state, RecordingStates.COMPLETE)
        self.assertEqual(self.recording.transcription_state, RecordingTranscriptionStates.NOT_STARTED)
        self.assertEqual(self.recording.transcription_failure_data, None)

        # Verify that the recording has an utterance
        self.assertEqual(self.recording.utterances.count(), 0)

        # Verify chat message was processed
        chat_messages = ChatMessage.objects.filter(bot=self.bot)
        self.assertEqual(chat_messages.count(), 1)
        chat_message = chat_messages.first()
        self.assertEqual(chat_message.text, "Hello bot from Test User! Sent while resumed")
        self.assertEqual(chat_message.participant.full_name, "Test User")
        self.assertEqual(chat_message.source_uuid, self.recording.object_id + "-test_chat_message_id_002")
        self.assertEqual(chat_message.to, ChatMessageToOptions.ONLY_BOT)

        # Cleanup
        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

        # Verify that the bot has participants
        self.assertEqual(self.bot.participants.count(), 2)
        bot_participant = self.bot.participants.get(user_uuid="bot_persistent_id")
        self.assertEqual(bot_participant.full_name, "Bot User")
        other_participant = self.bot.participants.get(user_uuid="test_persistent_id_123")
        self.assertEqual(other_participant.full_name, "Test User")

        # Verify that the expected participant events were created
        participant_events = ParticipantEvent.objects.filter(participant__bot=self.bot, participant__user_uuid="test_persistent_id_123")
        self.assertEqual(participant_events.count(), 2)
        self.assertEqual(participant_events[0].event_type, ParticipantEventTypes.JOIN)
        self.assertEqual(participant_events[1].event_type, ParticipantEventTypes.LEAVE)

        bot_participant_events = ParticipantEvent.objects.filter(participant__bot=self.bot, participant__user_uuid="bot_persistent_id")
        self.assertEqual(bot_participant_events.count(), 1)
        self.assertEqual(bot_participant_events[0].event_type, ParticipantEventTypes.JOIN)

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("deepgram.DeepgramClient")
    def test_recording_permission_denied_by_host(
        self,
        MockDeepgramClient,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        # Avoid any external calls
        MockDeepgramClient.return_value = create_mock_deepgram()

        mock_uploader = create_mock_file_uploader()
        MockFileUploader.return_value = mock_uploader

        mock_jwt.encode.return_value = "fake_jwt_token"

        controller = BotController(self.bot.id)

        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_join_and_permission_denied():
            from bots.bot_adapter import BotAdapter

            adapter = controller.adapter

            # Simulate successful auth
            adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)

            # Enter connecting state
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Ensure CanStartRawRecording fails so we request permission instead of granting immediately
            adapter.meeting_service.GetMeetingRecordingController.return_value.CanStartRawRecording.return_value = mock_zoom_sdk_adapter.SDKError.SDKERR_INTERNAL_ERROR

            # Enter in-meeting state (triggers on_join and recording privilege request)
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Give time for on_join setup
            time.sleep(1)

            # Simulate host denying local recording permission
            adapter.handle_recording_permission_denied(BotAdapter.BOT_RECORDING_PERMISSION_DENIED_REASON.HOST_DENIED_PERMISSION)

            # Allow message processing
            time.sleep(1)

            # Clean up connections in thread function if needed later
            connection.close()

        threading.Timer(2, simulate_join_and_permission_denied).start()

        bot_thread.join(timeout=10)

        # Verify adapter reflects paused state
        self.assertTrue(controller.adapter.recording_is_paused)

        # Verify bot state transitioned appropriately
        self.bot.refresh_from_db()
        self.assertEqual(self.bot.state, BotStates.JOINED_RECORDING_PERMISSION_DENIED)

        # Verify the specific BOT_RECORDING_PERMISSION_DENIED event exists with expected subtype
        denied_events = self.bot.bot_events.filter(
            event_type=BotEventTypes.BOT_RECORDING_PERMISSION_DENIED,
            event_sub_type=BotEventSubTypes.BOT_RECORDING_PERMISSION_DENIED_HOST_DENIED_PERMISSION,
        )
        self.assertTrue(denied_events.exists(), "Expected BOT_RECORDING_PERMISSION_DENIED event with HOST_DENIED_PERMISSION subtype")

        # Finish: simulate meeting ended to allow cleanup to run and thread to exit cleanly
        controller.adapter.meeting_service_event.onMeetingStatusChangedCallback(
            mock_zoom_sdk_adapter.MEETING_STATUS_ENDED,
            mock_zoom_sdk_adapter.SDKERR_SUCCESS,
        )

        controller.cleanup()
        bot_thread.join(timeout=5)

        # Verify Zoom SDK raw recording start/stop call counts:
        mock_recording_controller = controller.adapter.meeting_service.GetMeetingRecordingController.return_value
        self.assertEqual(mock_recording_controller.StartRawRecording.call_count, 0)
        self.assertEqual(mock_recording_controller.StopRawRecording.call_count, 0)

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("deepgram.DeepgramClient")
    def test_recording_permission_granted_then_paused_then_revoked_then_granted_again(
        self,
        MockDeepgramClient,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        # Avoid external calls
        MockDeepgramClient.return_value = create_mock_deepgram()
        MockFileUploader.return_value = create_mock_file_uploader()
        mock_jwt.encode.return_value = "fake_jwt_token"

        controller = BotController(self.bot.id)

        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_flow():
            from bots.bot_adapter import BotAdapter

            adapter = controller.adapter

            # Successful auth
            adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)

            # Connecting -> InMeeting
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Allow on_join to run; default mock grants permission immediately
            time.sleep(1)

            # Pause recording via controller
            send_sync_command(self.bot, "pause_recording")
            time.sleep(0.5)

            # Revoke permission from host
            adapter.handle_recording_permission_denied(BotAdapter.BOT_RECORDING_PERMISSION_DENIED_REASON.HOST_DENIED_PERMISSION)
            time.sleep(0.5)

            # Grant permission again
            adapter.handle_recording_permission_granted()
            time.sleep(0.5)

            # End meeting
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_ENDED,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            connection.close()

        threading.Timer(2, simulate_flow).start()

        bot_thread.join(timeout=12)

        # Validate adapter resumed recording
        self.assertFalse(controller.adapter.recording_is_paused)

        self.bot.refresh_from_db()

        # Verify ordered event sequence and state transitions
        events = list(self.bot.bot_events.order_by("created_at", "id"))
        self.assertEqual(len(events), 8)

        # 1) JOIN_REQUESTED: READY -> JOINING
        self.assertEqual(events[0].event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(events[0].old_state, BotStates.READY)
        self.assertEqual(events[0].new_state, BotStates.JOINING)

        # 2) BOT_JOINED_MEETING: JOINING -> JOINED_NOT_RECORDING
        self.assertEqual(events[1].event_type, BotEventTypes.BOT_JOINED_MEETING)
        self.assertEqual(events[1].old_state, BotStates.JOINING)
        self.assertEqual(events[1].new_state, BotStates.JOINED_NOT_RECORDING)

        # 3) BOT_RECORDING_PERMISSION_GRANTED: JOINED_NOT_RECORDING -> JOINED_RECORDING
        self.assertEqual(events[2].event_type, BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED)
        self.assertEqual(events[2].old_state, BotStates.JOINED_NOT_RECORDING)
        self.assertEqual(events[2].new_state, BotStates.JOINED_RECORDING)

        # 4) RECORDING_PAUSED: JOINED_RECORDING -> JOINED_RECORDING_PAUSED
        self.assertEqual(events[3].event_type, BotEventTypes.RECORDING_PAUSED)
        self.assertEqual(events[3].old_state, BotStates.JOINED_RECORDING)
        self.assertEqual(events[3].new_state, BotStates.JOINED_RECORDING_PAUSED)

        # 5) BOT_RECORDING_PERMISSION_DENIED: JOINED_RECORDING_PAUSED -> JOINED_RECORDING_PERMISSION_DENIED
        self.assertEqual(events[4].event_type, BotEventTypes.BOT_RECORDING_PERMISSION_DENIED)
        self.assertEqual(events[4].old_state, BotStates.JOINED_RECORDING_PAUSED)
        self.assertEqual(events[4].new_state, BotStates.JOINED_RECORDING_PERMISSION_DENIED)
        self.assertEqual(events[4].event_sub_type, BotEventSubTypes.BOT_RECORDING_PERMISSION_DENIED_HOST_DENIED_PERMISSION)

        # 6) BOT_RECORDING_PERMISSION_GRANTED: JOINED_RECORDING_PERMISSION_DENIED -> JOINED_RECORDING
        self.assertEqual(events[5].event_type, BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED)
        self.assertEqual(events[5].old_state, BotStates.JOINED_RECORDING_PERMISSION_DENIED)
        self.assertEqual(events[5].new_state, BotStates.JOINED_RECORDING)

        # 7) MEETING_ENDED: JOINED_RECORDING -> POST_PROCESSING
        self.assertEqual(events[6].event_type, BotEventTypes.MEETING_ENDED)
        self.assertEqual(events[6].old_state, BotStates.JOINED_RECORDING)
        self.assertEqual(events[6].new_state, BotStates.POST_PROCESSING)

        # 8) POST_PROCESSING_COMPLETED: POST_PROCESSING -> ENDED
        self.assertEqual(events[7].event_type, BotEventTypes.POST_PROCESSING_COMPLETED)
        self.assertEqual(events[7].old_state, BotStates.POST_PROCESSING)
        self.assertEqual(events[7].new_state, BotStates.ENDED)

        # Verify Zoom SDK raw recording start/stop call counts: Start twice (initial grant, re-grant), Stop once (pause)
        mock_recording_controller = controller.adapter.meeting_service.GetMeetingRecordingController.return_value
        self.assertEqual(mock_recording_controller.StartRawRecording.call_count, 2)
        self.assertEqual(mock_recording_controller.StopRawRecording.call_count, 1)

        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()

    @patch(
        "bots.zoom_bot_adapter.video_input_manager.zoom",
        new_callable=create_mock_zoom_sdk,
    )
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.zoom", new_callable=create_mock_zoom_sdk)
    @patch("bots.zoom_bot_adapter.zoom_bot_adapter.jwt")
    @patch("bots.bot_controller.bot_controller.FileUploader")
    @patch("deepgram.DeepgramClient")
    def test_recording_permission_granted_then_revoked_then_granted_again(
        self,
        MockDeepgramClient,
        MockFileUploader,
        mock_jwt,
        mock_zoom_sdk_adapter,
        mock_zoom_sdk_video,
    ):
        # Avoid external calls
        MockDeepgramClient.return_value = create_mock_deepgram()
        MockFileUploader.return_value = create_mock_file_uploader()
        mock_jwt.encode.return_value = "fake_jwt_token"

        controller = BotController(self.bot.id)

        bot_thread = threading.Thread(target=controller.run)
        bot_thread.daemon = True
        bot_thread.start()

        def simulate_flow():
            from bots.bot_adapter import BotAdapter

            adapter = controller.adapter

            # Successful auth
            adapter.auth_event.onAuthenticationReturnCallback(mock_zoom_sdk_adapter.AUTHRET_SUCCESS)

            # Connecting -> InMeeting
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_CONNECTING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_INMEETING,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            # Allow on_join to run; default mock grants permission immediately
            time.sleep(1)

            # Revoke permission from host
            adapter.handle_recording_permission_denied(BotAdapter.BOT_RECORDING_PERMISSION_DENIED_REASON.HOST_DENIED_PERMISSION)
            time.sleep(0.5)

            # Grant permission again
            adapter.handle_recording_permission_granted()
            time.sleep(0.5)

            # End meeting
            adapter.meeting_service_event.onMeetingStatusChangedCallback(
                mock_zoom_sdk_adapter.MEETING_STATUS_ENDED,
                mock_zoom_sdk_adapter.SDKERR_SUCCESS,
            )

            connection.close()

        threading.Timer(2, simulate_flow).start()

        bot_thread.join(timeout=12)

        # Validate adapter resumed recording
        self.assertFalse(controller.adapter.recording_is_paused)

        self.bot.refresh_from_db()

        # Verify ordered event sequence and state transitions
        events = list(self.bot.bot_events.order_by("created_at", "id"))
        self.assertEqual(len(events), 7)

        # 1) JOIN_REQUESTED: READY -> JOINING
        self.assertEqual(events[0].event_type, BotEventTypes.JOIN_REQUESTED)
        self.assertEqual(events[0].old_state, BotStates.READY)
        self.assertEqual(events[0].new_state, BotStates.JOINING)

        # 2) BOT_JOINED_MEETING: JOINING -> JOINED_NOT_RECORDING
        self.assertEqual(events[1].event_type, BotEventTypes.BOT_JOINED_MEETING)
        self.assertEqual(events[1].old_state, BotStates.JOINING)
        self.assertEqual(events[1].new_state, BotStates.JOINED_NOT_RECORDING)

        # 3) BOT_RECORDING_PERMISSION_GRANTED: JOINED_NOT_RECORDING -> JOINED_RECORDING
        self.assertEqual(events[2].event_type, BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED)
        self.assertEqual(events[2].old_state, BotStates.JOINED_NOT_RECORDING)
        self.assertEqual(events[2].new_state, BotStates.JOINED_RECORDING)

        # 5) BOT_RECORDING_PERMISSION_DENIED: JOINED_RECORDING -> JOINED_RECORDING_PERMISSION_DENIED
        self.assertEqual(events[3].event_type, BotEventTypes.BOT_RECORDING_PERMISSION_DENIED)
        self.assertEqual(events[3].old_state, BotStates.JOINED_RECORDING)
        self.assertEqual(events[3].new_state, BotStates.JOINED_RECORDING_PERMISSION_DENIED)
        self.assertEqual(events[3].event_sub_type, BotEventSubTypes.BOT_RECORDING_PERMISSION_DENIED_HOST_DENIED_PERMISSION)

        # 6) BOT_RECORDING_PERMISSION_GRANTED: JOINED_RECORDING_PERMISSION_DENIED -> JOINED_RECORDING
        self.assertEqual(events[4].event_type, BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED)
        self.assertEqual(events[4].old_state, BotStates.JOINED_RECORDING_PERMISSION_DENIED)
        self.assertEqual(events[4].new_state, BotStates.JOINED_RECORDING)

        # 7) MEETING_ENDED: JOINED_RECORDING -> POST_PROCESSING
        self.assertEqual(events[5].event_type, BotEventTypes.MEETING_ENDED)
        self.assertEqual(events[5].old_state, BotStates.JOINED_RECORDING)
        self.assertEqual(events[5].new_state, BotStates.POST_PROCESSING)

        # 8) POST_PROCESSING_COMPLETED: POST_PROCESSING -> ENDED
        self.assertEqual(events[6].event_type, BotEventTypes.POST_PROCESSING_COMPLETED)
        self.assertEqual(events[6].old_state, BotStates.POST_PROCESSING)
        self.assertEqual(events[6].new_state, BotStates.ENDED)

        # Verify Zoom SDK raw recording start/stop call counts: Start twice (initial grant, re-grant), Stop once (revoked)
        mock_recording_controller = controller.adapter.meeting_service.GetMeetingRecordingController.return_value
        self.assertEqual(mock_recording_controller.StartRawRecording.call_count, 2)
        self.assertEqual(mock_recording_controller.StopRawRecording.call_count, 1)

        controller.cleanup()
        bot_thread.join(timeout=5)

        # Close the database connection since we're in a thread
        connection.close()
