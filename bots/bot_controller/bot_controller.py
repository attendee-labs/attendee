import json
import logging
import os
import signal
import threading
import time
import traceback
from base64 import b64decode
from datetime import timedelta

import gi
import redis
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from bots.automatic_leave_configuration import AutomaticLeaveConfiguration
from bots.bot_adapter import BotAdapter
from bots.bot_controller.bot_websocket_client import BotWebsocketClient
from bots.bot_sso_utils import create_google_meet_sign_in_session
from bots.bots_api_utils import BotCreationSource
from bots.external_callback_utils import get_zoom_tokens
from bots.meeting_url_utils import meeting_type_from_url
from bots.models import (
    AudioChunk,
    Bot,
    BotChatMessageRequestManager,
    BotChatMessageRequestStates,
    BotDebugScreenshot,
    BotEventManager,
    BotEventSubTypes,
    BotEventTypes,
    BotMediaRequestManager,
    BotMediaRequestMediaTypes,
    BotMediaRequestStates,
    BotStates,
    ChatMessage,
    ChatMessageToOptions,
    Credentials,
    GoogleMeetBotLogin,
    GoogleMeetBotLoginGroup,
    MeetingTypes,
    Participant,
    ParticipantEvent,
    ParticipantEventTypes,
    RealtimeTriggerTypes,
    Recording,
    RecordingFormats,
    RecordingManager,
    RecordingTypes,
    TranscriptionProviders,
    Utterance,
    WebhookTriggerTypes,
)
from bots.webhook_payloads import chat_message_webhook_payload, participant_event_webhook_payload, utterance_webhook_payload
from bots.webhook_utils import trigger_webhook
from bots.websocket_payloads import mixed_audio_websocket_payload
from bots.zoom_oauth_connections_utils import get_zoom_tokens_via_zoom_oauth_app

from .audio_output_manager import AudioOutputManager
from .azure_file_uploader import AzureFileUploader
from .bot_resource_snapshot_taker import BotResourceSnapshotTaker
from .closed_caption_manager import ClosedCaptionManager
from .grouped_closed_caption_manager import GroupedClosedCaptionManager
from .gstreamer_pipeline import GstreamerPipeline
from .per_participant_non_streaming_audio_input_manager import PerParticipantNonStreamingAudioInputManager
from .per_participant_streaming_audio_input_manager import PerParticipantStreamingAudioInputManager
from .pipeline_configuration import PipelineConfiguration
from .realtime_audio_output_manager import RealtimeAudioOutputManager
from .rtmp_client import RTMPClient
from .s3_file_uploader import S3FileUploader
from .screen_and_audio_recorder import ScreenAndAudioRecorder
from .video_output_manager import VideoOutputManager
from .webpage_streamer_manager import WebpageStreamerManager

gi.require_version("GLib", "2.0")
from gi.repository import GLib

logger = logging.getLogger(__name__)


class BotController:
    # Default wait time for utterance termination (5 minutes)
    UTTERANCE_TERMINATION_WAIT_TIME_SECONDS = 300

    def use_streaming_transcription(self):
        provider = self.get_recording_transcription_provider()
        if provider == TranscriptionProviders.KYUTAI:
            return True
        if provider == TranscriptionProviders.DEEPGRAM:
            return self.bot_in_db.transcription_settings.deepgram_use_streaming()
        return False

    def per_participant_audio_input_manager(self):
        # Use streaming manager for providers that support streaming
        if self.use_streaming_transcription():
            return self.per_participant_streaming_audio_input_manager
        else:
            return self.per_participant_non_streaming_audio_input_manager

    def save_utterances_for_individual_audio_chunks(self):
        return self.get_recording_transcription_provider() != TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM

    def save_utterances_for_closed_captions(self):
        return self.get_recording_transcription_provider() == TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM

    def should_capture_audio_chunks(self):
        return self.save_utterances_for_individual_audio_chunks() or self.bot_in_db.record_async_transcription_audio_chunks()

    def disable_incoming_video_for_web_bots(self):
        return not (self.pipeline_configuration.record_video or self.pipeline_configuration.rtmp_stream_video)

    def create_google_meet_bot_login_session(self):
        if not self.bot_in_db.google_meet_use_bot_login():
            return None
        first_google_meet_bot_login_group = GoogleMeetBotLoginGroup.objects.filter(project=self.bot_in_db.project).first()
        if not first_google_meet_bot_login_group:
            return None
        least_used_google_meet_bot_login = first_google_meet_bot_login_group.google_meet_bot_logins.order_by("last_used_at").first()
        if not least_used_google_meet_bot_login:
            return None
        least_used_google_meet_bot_login.last_used_at = timezone.now()
        least_used_google_meet_bot_login.save()
        session_id = create_google_meet_sign_in_session(self.bot_in_db, least_used_google_meet_bot_login)
        return {
            "session_id": session_id,
            "login_email": least_used_google_meet_bot_login.email,
        }

    def google_meet_bot_login_is_available(self):
        return self.bot_in_db.google_meet_use_bot_login() and GoogleMeetBotLogin.objects.filter(group__project=self.bot_in_db.project).exists()

    def get_google_meet_bot_adapter(self):
        from bots.google_meet_bot_adapter import GoogleMeetBotAdapter

        if self.should_capture_audio_chunks():
            add_audio_chunk_callback = self.per_participant_audio_input_manager().add_chunk
        else:
            add_audio_chunk_callback = None

        return GoogleMeetBotAdapter(
            display_name=self.bot_in_db.name,
            send_message_callback=self.on_message_from_adapter,
            add_audio_chunk_callback=add_audio_chunk_callback,
            meeting_url=self.bot_in_db.meeting_url,
            add_video_frame_callback=None,
            wants_any_video_frames_callback=None,
            add_mixed_audio_chunk_callback=self.add_mixed_audio_chunk_callback if self.pipeline_configuration.websocket_stream_audio else None,
            upsert_caption_callback=self.closed_caption_manager.upsert_caption if self.save_utterances_for_closed_captions() else None,
            upsert_chat_message_callback=self.on_new_chat_message,
            add_participant_event_callback=self.add_participant_event,
            automatic_leave_configuration=self.automatic_leave_configuration,
            add_encoded_mp4_chunk_callback=None,
            recording_view=self.bot_in_db.recording_view(),
            google_meet_closed_captions_language=self.bot_in_db.transcription_settings.google_meet_closed_captions_language(),
            should_create_debug_recording=self.bot_in_db.create_debug_recording(),
            start_recording_screen_callback=self.screen_and_audio_recorder.start_recording if self.screen_and_audio_recorder else None,
            stop_recording_screen_callback=self.screen_and_audio_recorder.stop_recording if self.screen_and_audio_recorder else None,
            video_frame_size=self.bot_in_db.recording_dimensions(),
            record_chat_messages_when_paused=self.bot_in_db.record_chat_messages_when_paused(),
            disable_incoming_video=self.disable_incoming_video_for_web_bots(),
            google_meet_bot_login_is_available=self.google_meet_bot_login_is_available(),
            google_meet_bot_login_should_be_used=self.bot_in_db.google_meet_login_mode_is_always(),
            create_google_meet_bot_login_session_callback=self.create_google_meet_bot_login_session,
        )

    def get_teams_bot_adapter(self):
        from bots.teams_bot_adapter import TeamsBotAdapter

        if self.should_capture_audio_chunks():
            add_audio_chunk_callback = self.per_participant_audio_input_manager().add_chunk
        else:
            add_audio_chunk_callback = None

        teams_bot_login_credentials = self.bot_in_db.project.credentials.filter(credential_type=Credentials.CredentialTypes.TEAMS_BOT_LOGIN).first()

        return TeamsBotAdapter(
            display_name=self.bot_in_db.name,
            send_message_callback=self.on_message_from_adapter,
            add_audio_chunk_callback=add_audio_chunk_callback,
            meeting_url=self.bot_in_db.meeting_url,
            add_video_frame_callback=None,
            wants_any_video_frames_callback=None,
            add_mixed_audio_chunk_callback=self.add_mixed_audio_chunk_callback if self.pipeline_configuration.websocket_stream_audio else None,
            upsert_caption_callback=self.closed_caption_manager.upsert_caption if self.save_utterances_for_closed_captions() else None,
            upsert_chat_message_callback=self.on_new_chat_message,
            add_participant_event_callback=self.add_participant_event,
            automatic_leave_configuration=self.automatic_leave_configuration,
            add_encoded_mp4_chunk_callback=None,
            recording_view=self.bot_in_db.recording_view(),
            teams_closed_captions_language=self.bot_in_db.transcription_settings.teams_closed_captions_language(),
            should_create_debug_recording=self.bot_in_db.create_debug_recording(),
            start_recording_screen_callback=self.screen_and_audio_recorder.start_recording if self.screen_and_audio_recorder else None,
            stop_recording_screen_callback=self.screen_and_audio_recorder.stop_recording if self.screen_and_audio_recorder else None,
            video_frame_size=self.bot_in_db.recording_dimensions(),
            teams_bot_login_credentials=teams_bot_login_credentials.get_credentials() if teams_bot_login_credentials and self.bot_in_db.teams_use_bot_login() else None,
            record_chat_messages_when_paused=self.bot_in_db.record_chat_messages_when_paused(),
            disable_incoming_video=self.disable_incoming_video_for_web_bots(),
        )

    def get_zoom_oauth_credentials_via_credentials_record(self):
        zoom_oauth_credentials_record = self.bot_in_db.project.credentials.filter(credential_type=Credentials.CredentialTypes.ZOOM_OAUTH).first()
        if not zoom_oauth_credentials_record:
            raise Exception("Zoom OAuth credentials not found")

        zoom_oauth_credentials = zoom_oauth_credentials_record.get_credentials()
        if not zoom_oauth_credentials:
            raise Exception("Zoom OAuth credentials data not found")

        return zoom_oauth_credentials

    def get_zoom_oauth_credentials_via_zoom_oauth_app(self):
        zoom_oauth_app = self.bot_in_db.project.zoom_oauth_apps.first()
        if not zoom_oauth_app:
            return

        return {"client_id": zoom_oauth_app.client_id, "client_secret": zoom_oauth_app.client_secret}

    def get_zoom_oauth_credentials_and_tokens(self):
        zoom_oauth_credentials = self.get_zoom_oauth_credentials_via_zoom_oauth_app() or self.get_zoom_oauth_credentials_via_credentials_record()

        zoom_tokens = {}
        if self.bot_in_db.zoom_tokens_callback_url():
            zoom_tokens = get_zoom_tokens(self.bot_in_db)
        else:
            zoom_tokens = get_zoom_tokens_via_zoom_oauth_app(self.bot_in_db)

        return zoom_oauth_credentials, zoom_tokens

    def get_zoom_web_bot_adapter(self):
        from bots.zoom_web_bot_adapter import ZoomWebBotAdapter

        if self.should_capture_audio_chunks():
            add_audio_chunk_callback = self.per_participant_audio_input_manager().add_chunk
        else:
            add_audio_chunk_callback = None

        zoom_oauth_credentials, zoom_tokens = self.get_zoom_oauth_credentials_and_tokens()

        return ZoomWebBotAdapter(
            display_name=self.bot_in_db.name,
            send_message_callback=self.on_message_from_adapter,
            add_audio_chunk_callback=add_audio_chunk_callback,
            meeting_url=self.bot_in_db.meeting_url,
            add_video_frame_callback=None,
            wants_any_video_frames_callback=None,
            add_mixed_audio_chunk_callback=self.add_mixed_audio_chunk_callback if self.pipeline_configuration.websocket_stream_audio else None,
            upsert_caption_callback=self.closed_caption_manager.upsert_caption if self.save_utterances_for_closed_captions() else None,
            upsert_chat_message_callback=self.on_new_chat_message,
            add_participant_event_callback=self.add_participant_event,
            automatic_leave_configuration=self.automatic_leave_configuration,
            add_encoded_mp4_chunk_callback=None,
            recording_view=self.bot_in_db.recording_view(),
            should_create_debug_recording=self.bot_in_db.create_debug_recording(),
            start_recording_screen_callback=self.screen_and_audio_recorder.start_recording if self.screen_and_audio_recorder else None,
            stop_recording_screen_callback=self.screen_and_audio_recorder.stop_recording if self.screen_and_audio_recorder else None,
            video_frame_size=self.bot_in_db.recording_dimensions(),
            zoom_client_id=zoom_oauth_credentials["client_id"],
            zoom_client_secret=zoom_oauth_credentials["client_secret"],
            zoom_closed_captions_language=self.bot_in_db.transcription_settings.zoom_closed_captions_language(),
            should_ask_for_recording_permission=self.pipeline_configuration.record_audio or self.pipeline_configuration.rtmp_stream_audio or self.pipeline_configuration.websocket_stream_audio or self.pipeline_configuration.record_video or self.pipeline_configuration.rtmp_stream_video,
            record_chat_messages_when_paused=self.bot_in_db.record_chat_messages_when_paused(),
            disable_incoming_video=self.disable_incoming_video_for_web_bots(),
            zoom_tokens=zoom_tokens,
        )

    def get_zoom_bot_adapter(self):
        from bots.zoom_bot_adapter import ZoomBotAdapter

        add_audio_chunk_callback = self.per_participant_audio_input_manager().add_chunk

        zoom_oauth_credentials, zoom_tokens = self.get_zoom_oauth_credentials_and_tokens()

        return ZoomBotAdapter(
            use_one_way_audio=self.pipeline_configuration.transcribe_audio,
            use_mixed_audio=self.pipeline_configuration.record_audio or self.pipeline_configuration.rtmp_stream_audio or self.pipeline_configuration.websocket_stream_audio,
            use_video=self.pipeline_configuration.record_video or self.pipeline_configuration.rtmp_stream_video,
            display_name=self.bot_in_db.name,
            send_message_callback=self.on_message_from_adapter,
            add_audio_chunk_callback=add_audio_chunk_callback,
            zoom_client_id=zoom_oauth_credentials["client_id"],
            zoom_client_secret=zoom_oauth_credentials["client_secret"],
            meeting_url=self.bot_in_db.meeting_url,
            add_video_frame_callback=self.gstreamer_pipeline.on_new_video_frame if self.gstreamer_pipeline else None,
            wants_any_video_frames_callback=self.gstreamer_pipeline.wants_any_video_frames if self.gstreamer_pipeline else lambda: False,
            add_mixed_audio_chunk_callback=self.add_mixed_audio_chunk_callback,
            upsert_chat_message_callback=self.on_new_chat_message,
            add_participant_event_callback=self.add_participant_event,
            automatic_leave_configuration=self.automatic_leave_configuration,
            video_frame_size=self.bot_in_db.recording_dimensions(),
            zoom_tokens=zoom_tokens,
            zoom_meeting_settings=self.bot_in_db.zoom_meeting_settings(),
            record_chat_messages_when_paused=self.bot_in_db.record_chat_messages_when_paused(),
        )

    def add_mixed_audio_chunk_callback(self, chunk: bytes):
        if self.gstreamer_pipeline:
            self.gstreamer_pipeline.on_mixed_audio_raw_data_received_callback(chunk)

        if not self.websocket_audio_client:
            return

        if not self.websocket_audio_client.started():
            logger.info("Starting websocket audio client...")
            self.websocket_audio_client.start()

        payload = mixed_audio_websocket_payload(
            chunk=chunk,
            input_sample_rate=self.mixed_audio_sample_rate(),
            output_sample_rate=self.bot_in_db.websocket_audio_sample_rate(),
            bot_object_id=self.bot_in_db.object_id,
        )

        self.websocket_audio_client.send_async(payload)

    def get_meeting_type(self):
        meeting_type = meeting_type_from_url(self.bot_in_db.meeting_url)
        if meeting_type is None:
            raise Exception(f"Could not determine meeting type for meeting url {self.bot_in_db.meeting_url}")
        return meeting_type

    def get_per_participant_audio_utterance_delay_ms(self):
        meeting_type = self.get_meeting_type()
        if meeting_type == MeetingTypes.TEAMS:
            return 2000
        if meeting_type == MeetingTypes.ZOOM and self.bot_in_db.use_zoom_web_adapter():
            return 2000
        return 0

    def get_per_participant_audio_sample_rate(self):
        meeting_type = self.get_meeting_type()
        if meeting_type == MeetingTypes.ZOOM:
            if self.bot_in_db.use_zoom_web_adapter():
                return 48000
            else:
                return 32000
        elif meeting_type == MeetingTypes.GOOGLE_MEET:
            return 48000
        elif meeting_type == MeetingTypes.TEAMS:
            return 48000

    def mixed_audio_sample_rate(self):
        meeting_type = self.get_meeting_type()
        if meeting_type == MeetingTypes.ZOOM:
            if self.bot_in_db.use_zoom_web_adapter():
                return 48000
            else:
                return 32000
        elif meeting_type == MeetingTypes.GOOGLE_MEET:
            return 48000
        elif meeting_type == MeetingTypes.TEAMS:
            return 48000

    def get_audio_format(self):
        meeting_type = self.get_meeting_type()
        if meeting_type == MeetingTypes.ZOOM:
            if self.bot_in_db.use_zoom_web_adapter():
                return GstreamerPipeline.AUDIO_FORMAT_FLOAT
            else:
                return GstreamerPipeline.AUDIO_FORMAT_PCM
        elif meeting_type == MeetingTypes.GOOGLE_MEET:
            return GstreamerPipeline.AUDIO_FORMAT_FLOAT
        elif meeting_type == MeetingTypes.TEAMS:
            return GstreamerPipeline.AUDIO_FORMAT_FLOAT

    def get_sleep_time_between_audio_output_chunks_seconds(self):
        meeting_type = self.get_meeting_type()
        if meeting_type == MeetingTypes.ZOOM:
            return 0.9
        return 0.1

    def get_bot_adapter(self):
        meeting_type = self.get_meeting_type()
        if meeting_type == MeetingTypes.ZOOM:
            if self.bot_in_db.use_zoom_web_adapter():
                return self.get_zoom_web_bot_adapter()
            else:
                return self.get_zoom_bot_adapter()
        elif meeting_type == MeetingTypes.GOOGLE_MEET:
            return self.get_google_meet_bot_adapter()
        elif meeting_type == MeetingTypes.TEAMS:
            return self.get_teams_bot_adapter()

    def get_first_buffer_timestamp_ms(self):
        if self.screen_and_audio_recorder:
            return self.adapter.get_first_buffer_timestamp_ms()

        if self.gstreamer_pipeline:
            if self.gstreamer_pipeline.start_time_ns is None:
                return None
            return int(self.gstreamer_pipeline.start_time_ns / 1_000_000) + self.adapter.get_first_buffer_timestamp_ms_offset()

    def recording_file_saved(self, s3_storage_key):
        recording = Recording.objects.get(bot=self.bot_in_db, is_default_recording=True)
        recording.file = s3_storage_key
        recording.first_buffer_timestamp_ms = self.get_first_buffer_timestamp_ms()
        recording.save()

    def get_recording_transcription_provider(self):
        recording = Recording.objects.get(bot=self.bot_in_db, is_default_recording=True)
        return recording.transcription_provider

    def get_recording_filename(self):
        recording = Recording.objects.get(bot=self.bot_in_db, is_default_recording=True)
        return f"{self.bot_in_db.object_id}-{recording.object_id}.{self.bot_in_db.recording_format()}"

    def on_rtmp_connection_failed(self):
        logger.info("RTMP connection failed")
        BotEventManager.create_event(
            bot=self.bot_in_db,
            event_type=BotEventTypes.FATAL_ERROR,
            event_sub_type=BotEventSubTypes.FATAL_ERROR_RTMP_CONNECTION_FAILED,
            event_metadata={"rtmp_destination_url": self.bot_in_db.rtmp_destination_url()},
        )
        self.cleanup()

    def on_new_sample_from_gstreamer_pipeline(self, data):
        # For now, we'll assume that if rtmp streaming is enabled, we don't need to upload to s3
        if self.rtmp_client:
            write_succeeded = self.rtmp_client.write_data(data)
            if not write_succeeded:
                GLib.idle_add(lambda: self.on_rtmp_connection_failed())
        else:
            raise Exception("No rtmp client found")

    def upload_recording_to_external_media_storage_if_enabled(self):
        if not self.bot_in_db.external_media_storage_bucket_name():
            return

        external_media_storage_credentials_record = self.bot_in_db.project.credentials.filter(credential_type=Credentials.CredentialTypes.EXTERNAL_MEDIA_STORAGE).first()
        if not external_media_storage_credentials_record:
            logger.error(f"No external media storage credentials found for bot {self.bot_in_db.id}")
            return

        external_media_storage_credentials = external_media_storage_credentials_record.get_credentials()
        if not external_media_storage_credentials:
            logger.error(f"External media storage credentials data not found for bot {self.bot_in_db.id}")
            return

        try:
            logger.info(f"Uploading recording to external media storage bucket {self.bot_in_db.external_media_storage_bucket_name()}")
            file_uploader = S3FileUploader(
                bucket=self.bot_in_db.external_media_storage_bucket_name(),
                filename=self.bot_in_db.external_media_storage_recording_file_name() or self.get_recording_filename(),
                endpoint_url=external_media_storage_credentials.get("endpoint_url") or None,
                region_name=external_media_storage_credentials.get("region_name"),
                access_key_id=external_media_storage_credentials.get("access_key_id"),
                access_key_secret=external_media_storage_credentials.get("access_key_secret"),
            )
            file_uploader.upload_file(self.get_recording_file_location())
            file_uploader.wait_for_upload()
            logger.info(f"File uploader finished uploading file to external media storage bucket {self.bot_in_db.external_media_storage_bucket_name()}")
        except Exception as e:
            logger.exception(f"Error uploading recording to external media storage bucket {self.bot_in_db.external_media_storage_bucket_name()}: {e}")

    def get_file_uploader(self):
        if settings.STORAGE_PROTOCOL == "azure":
            return AzureFileUploader(
                container=settings.AZURE_RECORDING_STORAGE_CONTAINER_NAME,
                filename=self.get_recording_filename(),
                connection_string=settings.RECORDING_STORAGE_BACKEND.get("OPTIONS").get("connection_string"),
                account_key=settings.RECORDING_STORAGE_BACKEND.get("OPTIONS").get("account_key"),
                account_name=settings.RECORDING_STORAGE_BACKEND.get("OPTIONS").get("account_name"),
            )

        return S3FileUploader(
            bucket=settings.AWS_RECORDING_STORAGE_BUCKET_NAME,
            filename=self.get_recording_filename(),
            endpoint_url=settings.RECORDING_STORAGE_BACKEND.get("OPTIONS").get("endpoint_url"),
        )

    def cleanup(self):
        if self.cleanup_called:
            logger.info("Cleanup already called, exiting")
            return
        self.cleanup_called = True

        normal_quitting_process_worked = False
        import threading

        def terminate_worker():
            import time

            time.sleep(600)
            if normal_quitting_process_worked:
                logger.info("Normal quitting process worked, not force terminating worker")
                return
            logger.info("Terminating worker with hard timeout...")
            os.kill(os.getpid(), signal.SIGKILL)  # Force terminate the worker process

        termination_thread = threading.Thread(target=terminate_worker, daemon=True)
        termination_thread.start()

        if self.gstreamer_pipeline:
            logger.info("Telling gstreamer pipeline to cleanup...")
            self.gstreamer_pipeline.cleanup()

        if self.rtmp_client:
            logger.info("Telling rtmp client to cleanup...")
            self.rtmp_client.stop()

        if self.adapter:
            logger.info("Telling adapter to leave meeting...")
            self.adapter.leave()
            logger.info("Telling adapter to cleanup...")
            self.adapter.cleanup()

        if self.main_loop and self.main_loop.is_running():
            self.main_loop.quit()

        if self.screen_and_audio_recorder:
            logger.info("Telling media recorder receiver to cleanup...")
            self.screen_and_audio_recorder.cleanup()

        if self.realtime_audio_output_manager:
            logger.info("Telling realtime audio output manager to cleanup...")
            self.realtime_audio_output_manager.cleanup()

        if self.webpage_streamer_manager:
            logger.info("Telling webpage streamer manager to cleanup...")
            self.webpage_streamer_manager.cleanup()

        if self.websocket_audio_client:
            logger.info("Telling websocket audio client to cleanup...")
            self.websocket_audio_client.cleanup()

        if self.get_recording_file_location():
            self.upload_recording_to_external_media_storage_if_enabled()

            logger.info("Telling file uploader to upload recording file...")
            file_uploader = self.get_file_uploader()
            file_uploader.upload_file(self.get_recording_file_location())
            file_uploader.wait_for_upload()
            logger.info("File uploader finished uploading file")
            file_uploader.delete_file(self.get_recording_file_location())
            logger.info("File uploader deleted file from local filesystem")
            self.recording_file_saved(file_uploader.filename)

        if self.bot_in_db.create_debug_recording():
            self.save_debug_recording()

        if self.bot_in_db.state == BotStates.POST_PROCESSING:
            self.wait_until_all_utterances_are_terminated()
            BotEventManager.create_event(bot=self.bot_in_db, event_type=BotEventTypes.POST_PROCESSING_COMPLETED)

        normal_quitting_process_worked = True

    # We're going to wait until all utterances are transcribed or have failed. If there are still
    # in progress utterances, after 5 minutes, then we'll consider them failed and mark them as timed out.
    def wait_until_all_utterances_are_terminated(self):
        default_recording = self.bot_in_db.recordings.get(is_default_recording=True)

        start_time = time.time()
        wait_time_seconds = self.UTTERANCE_TERMINATION_WAIT_TIME_SECONDS
        while time.time() - start_time < wait_time_seconds:
            in_progress_utterances = default_recording.utterances.filter(transcription__isnull=True, failure_data__isnull=True)
            # If no more in progress utterances, then we're done
            if not in_progress_utterances.exists():
                logger.info(f"All utterances are terminated for bot {self.bot_in_db.id}")
                return

            logger.info(f"Waiting for {len(in_progress_utterances)} utterances to terminate. It has been {time.time() - start_time} seconds. We will wait {wait_time_seconds} seconds.")
            time.sleep(5)

        logger.info(f"Timed out in post-processing waiting for utterances to terminate for bot {self.bot_in_db.id}. Transcription will be marked as failed because recording terminated.")

    def __init__(self, bot_id):
        self.bot_in_db = Bot.objects.get(id=bot_id)
        self.cleanup_called = False
        self.run_called = False

        self.redis_client = None
        self.pubsub = None
        self.pubsub_channel = f"bot_{self.bot_in_db.id}"

        self.automatic_leave_configuration = AutomaticLeaveConfiguration(**self.bot_in_db.automatic_leave_settings())

        self.pipeline_configuration = self.get_pipeline_configuration()

    def get_pipeline_configuration(self):
        # This is sloppy, we won't be able to rely on these predefined configurations forever, but it will be ok for now

        if self.bot_in_db.rtmp_destination_url():
            return PipelineConfiguration.rtmp_streaming_bot()

        if self.bot_in_db.recording_type() == RecordingTypes.AUDIO_ONLY:
            if self.bot_in_db.websocket_audio_url():
                return PipelineConfiguration.audio_recorder_bot_with_websocket_audio()
            else:
                return PipelineConfiguration.audio_recorder_bot()

        if self.bot_in_db.recording_type() == RecordingTypes.NO_RECORDING:
            if self.bot_in_db.websocket_audio_url():
                return PipelineConfiguration.pure_transcription_bot_with_websocket_audio()
            else:
                return PipelineConfiguration.pure_transcription_bot()

        if self.bot_in_db.websocket_audio_url():
            return PipelineConfiguration.recorder_bot_with_websocket_audio()

        return PipelineConfiguration.recorder_bot()

    def get_gstreamer_sink_type(self):
        if self.pipeline_configuration.rtmp_stream_audio or self.pipeline_configuration.rtmp_stream_video:
            return GstreamerPipeline.SINK_TYPE_APPSINK
        else:
            return GstreamerPipeline.SINK_TYPE_FILE

    def get_gstreamer_output_format(self):
        if self.pipeline_configuration.rtmp_stream_audio or self.pipeline_configuration.rtmp_stream_video:
            return GstreamerPipeline.OUTPUT_FORMAT_FLV

        if self.bot_in_db.recording_format() == RecordingFormats.WEBM:
            return GstreamerPipeline.OUTPUT_FORMAT_WEBM
        elif self.bot_in_db.recording_format() == RecordingFormats.MP3:
            return GstreamerPipeline.OUTPUT_FORMAT_MP3
        else:
            return GstreamerPipeline.OUTPUT_FORMAT_MP4

    def get_recording_file_location(self):
        if self.pipeline_configuration.rtmp_stream_audio or self.pipeline_configuration.rtmp_stream_video:
            return None
        elif not self.pipeline_configuration.record_audio and not self.pipeline_configuration.record_video:
            return None
        else:
            return os.path.join(self.get_recording_storage_directory(), self.get_recording_filename())

    def get_recording_storage_directory(self):
        if self.bot_in_db.reserve_additional_storage():
            return "/bot-persistent-storage"
        else:
            return "/tmp"

    def should_create_gstreamer_pipeline(self):
        # if we're not recording audio or video and not doing rtmp streaming, then we don't need to create a gstreamer pipeline
        if not self.pipeline_configuration.record_audio and not self.pipeline_configuration.record_video and not self.pipeline_configuration.rtmp_stream_audio and not self.pipeline_configuration.rtmp_stream_video:
            return False

        # For google meet / teams, we're doing a media recorder based recording technique that does the video processing in the browser
        # so we don't need to create a gstreamer pipeline here
        meeting_type = self.get_meeting_type()
        if meeting_type == MeetingTypes.ZOOM:
            if self.bot_in_db.use_zoom_web_adapter():
                return False
            else:
                return True
        elif meeting_type == MeetingTypes.GOOGLE_MEET:
            return False
        elif meeting_type == MeetingTypes.TEAMS:
            return False

    def should_create_websocket_client(self):
        return self.pipeline_configuration.websocket_stream_audio

    def should_create_screen_and_audio_recorder(self):
        # if we're not recording audio or video and not doing rtmp streaming, then we don't need to create a screen and audio recorder
        if not self.pipeline_configuration.record_audio and not self.pipeline_configuration.record_video and not self.pipeline_configuration.rtmp_stream_audio and not self.pipeline_configuration.rtmp_stream_video:
            return False

        return not self.should_create_gstreamer_pipeline()

    def connect_to_redis(self):
        # Close both pubsub and client if they exist
        if self.pubsub:
            self.pubsub.close()
        if self.redis_client:
            self.redis_client.close()

        redis_url = os.getenv("REDIS_URL") + ("?ssl_cert_reqs=none" if os.getenv("DISABLE_REDIS_SSL") else "")
        self.redis_client = redis.from_url(redis_url)
        self.pubsub = self.redis_client.pubsub()
        self.pubsub.subscribe(self.pubsub_channel)
        logger.info(f"Redis connection established for bot {self.bot_in_db.id}")

    # Sarvam has this 30 second limit on audio clips, so we need to change the max utterance duration to 30 seconds
    # and make the silence duration lower so it generates a bunch of small clips
    def non_streaming_audio_utterance_size_limit(self):
        if self.get_recording_transcription_provider() == TranscriptionProviders.SARVAM:
            return 1920000  # 30 seconds of audio at 32kHz
        else:
            return 19200000  # 19.2 MB / 2 bytes per sample / 32,000 samples per second = 300 seconds of continuous audio

    def non_streaming_audio_silence_duration_limit(self):
        if self.get_recording_transcription_provider() == TranscriptionProviders.SARVAM:
            return 1  # seconds
        else:
            return 3  # seconds

    def run(self):
        if self.run_called:
            raise Exception("Run already called, exiting")
        self.run_called = True

        self.connect_to_redis()

        # Initialize core objects
        # Only used for adapters that can provide per-participant audio

        self.per_participant_non_streaming_audio_input_manager = PerParticipantNonStreamingAudioInputManager(
            save_audio_chunk_callback=self.process_individual_audio_chunk,
            get_participant_callback=self.get_participant,
            sample_rate=self.get_per_participant_audio_sample_rate(),
            utterance_size_limit=self.non_streaming_audio_utterance_size_limit(),
            silence_duration_limit=self.non_streaming_audio_silence_duration_limit(),
            should_print_diagnostic_info=self.should_capture_audio_chunks(),
        )

        self.per_participant_streaming_audio_input_manager = PerParticipantStreamingAudioInputManager(
            get_participant_callback=self.get_participant,
            sample_rate=self.get_per_participant_audio_sample_rate(),
            transcription_provider=self.get_recording_transcription_provider(),
            bot=self.bot_in_db,
        )

        # Only used for adapters that can provide closed captions
        if self.bot_in_db.transcription_settings.meeting_closed_captions_merge_consecutive_captions():
            self.closed_caption_manager = GroupedClosedCaptionManager(
                save_utterance_callback=self.save_closed_caption_utterance,
                get_participant_callback=self.get_participant,
            )
        else:
            self.closed_caption_manager = ClosedCaptionManager(
                save_utterance_callback=self.save_closed_caption_utterance,
                get_participant_callback=self.get_participant,
            )

        self.rtmp_client = None
        if self.pipeline_configuration.rtmp_stream_audio or self.pipeline_configuration.rtmp_stream_video:
            self.rtmp_client = RTMPClient(rtmp_url=self.bot_in_db.rtmp_destination_url())
            self.rtmp_client.start()

        self.gstreamer_pipeline = None
        if self.should_create_gstreamer_pipeline():
            self.gstreamer_pipeline = GstreamerPipeline(
                on_new_sample_callback=self.on_new_sample_from_gstreamer_pipeline,
                video_frame_size=self.bot_in_db.recording_dimensions(),
                audio_format=self.get_audio_format(),
                output_format=self.get_gstreamer_output_format(),
                sink_type=self.get_gstreamer_sink_type(),
                file_location=self.get_recording_file_location(),
            )
            self.gstreamer_pipeline.setup()

        self.screen_and_audio_recorder = None
        if self.should_create_screen_and_audio_recorder():
            self.screen_and_audio_recorder = ScreenAndAudioRecorder(
                file_location=self.get_recording_file_location(),
                recording_dimensions=self.bot_in_db.recording_dimensions(),
                audio_only=not (self.pipeline_configuration.record_video or self.pipeline_configuration.rtmp_stream_video),
            )

        self.websocket_audio_client = None
        if self.should_create_websocket_client():
            self.websocket_audio_client = BotWebsocketClient(
                url=self.bot_in_db.websocket_audio_url(),
                on_message_callback=self.on_message_from_websocket_audio,
            )

        self.adapter = self.get_bot_adapter()

        self.audio_output_manager = AudioOutputManager(
            currently_playing_audio_media_request_finished_callback=self.currently_playing_audio_media_request_finished,
            play_raw_audio_callback=self.adapter.send_raw_audio,
            sleep_time_between_chunks_seconds=self.get_sleep_time_between_audio_output_chunks_seconds(),
        )

        self.realtime_audio_output_manager = RealtimeAudioOutputManager(
            play_raw_audio_callback=self.adapter.send_raw_audio,
            sleep_time_between_chunks_seconds=self.get_sleep_time_between_audio_output_chunks_seconds(),
            output_sample_rate=self.mixed_audio_sample_rate(),
        )

        self.video_output_manager = VideoOutputManager(
            currently_playing_video_media_request_finished_callback=self.currently_playing_video_media_request_finished,
            check_if_currently_playing_video_media_request_is_still_playing_callback=self.adapter.is_sent_video_still_playing,
            play_video_callback=self.adapter.send_video,
        )

        self.webpage_streamer_manager = None
        if self.bot_in_db.should_launch_webpage_streamer():
            self.webpage_streamer_manager = WebpageStreamerManager(
                get_peer_connection_offer_callback=self.adapter.webpage_streamer_get_peer_connection_offer,
                start_peer_connection_callback=self.adapter.webpage_streamer_start_peer_connection,
                play_bot_output_media_stream_callback=self.adapter.webpage_streamer_play_bot_output_media_stream,
                stop_bot_output_media_stream_callback=self.adapter.webpage_streamer_stop_bot_output_media_stream,
                webpage_streamer_service_hostname=self.bot_in_db.k8s_webpage_streamer_service_hostname(),
            )

        self.bot_resource_snapshot_taker = BotResourceSnapshotTaker(self.bot_in_db)

        # Create GLib main loop
        self.main_loop = GLib.MainLoop()

        def repeatedly_try_to_reconnect_to_redis():
            reconnect_delay_seconds = 1
            num_attempts = 0
            while True:
                try:
                    self.connect_to_redis()
                    break
                except Exception as e:
                    logger.info(f"Error reconnecting to Redis: {e} Attempt {num_attempts} / 30.")
                    time.sleep(reconnect_delay_seconds)
                    num_attempts += 1
                    if num_attempts > 30:
                        raise Exception("Failed to reconnect to Redis after 30 attempts")

        def redis_listener():
            while True:
                try:
                    message = self.pubsub.get_message(timeout=1.0)
                    if message:
                        # Schedule Redis message handling in the main GLib loop
                        GLib.idle_add(self.handle_redis_message, message)
                except Exception as e:
                    # If this is a certain type of exception, we can attempt to reconnect
                    if isinstance(e, redis.exceptions.ConnectionError) and "Connection closed by server." in str(e):
                        logger.info("Redis connection closed by server. Attempting to reconnect...")
                        repeatedly_try_to_reconnect_to_redis()

                    else:
                        # log the type of exception
                        logger.info(f"Error in Redis listener: {type(e)} {e}")
                        break

        redis_thread = threading.Thread(target=redis_listener, daemon=True)
        redis_thread.start()

        # Add timeout just for audio processing
        self.first_timeout_call = True
        GLib.timeout_add(100, self.on_main_loop_timeout)

        # Add signal handlers so that when we get a SIGTERM or SIGINT, we can clean up the bot
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self.handle_glib_shutdown)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, self.handle_glib_shutdown)

        # Run the main loop
        try:
            self.main_loop.run()
        except Exception as e:
            logger.info(f"Error in bot {self.bot_in_db.id}: {str(e)}")
            self.cleanup()
        finally:
            # Clean up Redis subscription
            self.pubsub.unsubscribe(self.pubsub_channel)
            self.pubsub.close()

    def take_action_based_on_bot_in_db(self):
        if self.bot_in_db.state == BotStates.JOINING:
            logger.info("take_action_based_on_bot_in_db - JOINING")
            BotEventManager.set_requested_bot_action_taken_at(self.bot_in_db)
            self.adapter.init()
        if self.bot_in_db.state == BotStates.LEAVING:
            logger.info("take_action_based_on_bot_in_db - LEAVING")
            BotEventManager.set_requested_bot_action_taken_at(self.bot_in_db)
            self.adapter.leave()
        if self.bot_in_db.state == BotStates.STAGED:
            logger.info(f"take_action_based_on_bot_in_db - STAGED. For now, this is a no-op. join_at = {self.bot_in_db.join_at.isoformat()}")

    def join_if_staged_and_time_to_join(self):
        if self.bot_in_db.state != BotStates.STAGED:
            return
        if self.bot_in_db.join_at > timezone.now() + timedelta(seconds=self.adapter.get_staged_bot_join_delay_seconds()):
            return

        # Transition to JOINING
        logger.info(f"Joining bot {self.bot_in_db.id} ({self.bot_in_db.object_id}) because join_at is {self.bot_in_db.join_at.isoformat()} and current time is {timezone.now().isoformat()}")
        BotEventManager.create_event(
            bot=self.bot_in_db,
            event_type=BotEventTypes.JOIN_REQUESTED,
            event_metadata={"source": BotCreationSource.SCHEDULER},
        )
        self.take_action_based_on_bot_in_db()

    def get_participant(self, participant_id):
        return self.adapter.get_participant(participant_id)

    def currently_playing_audio_media_request_finished(self, audio_media_request):
        logger.info("currently_playing_audio_media_request_finished called")
        BotMediaRequestManager.set_media_request_finished(audio_media_request)
        self.take_action_based_on_audio_media_requests_in_db()

    def currently_playing_video_media_request_finished(self, video_media_request):
        logger.info("currently_playing_video_media_request_finished called")
        BotMediaRequestManager.set_media_request_finished(video_media_request)
        self.take_action_based_on_video_media_requests_in_db()

    def take_action_based_on_audio_media_requests_in_db(self):
        media_type = BotMediaRequestMediaTypes.AUDIO
        oldest_enqueued_media_request = self.bot_in_db.media_requests.filter(state=BotMediaRequestStates.ENQUEUED, media_type=media_type).order_by("created_at").first()
        if not oldest_enqueued_media_request:
            return
        currently_playing_media_request = self.bot_in_db.media_requests.filter(state=BotMediaRequestStates.PLAYING, media_type=media_type).first()
        if currently_playing_media_request:
            logger.info(f"Currently playing media request {currently_playing_media_request.id} so cannot play another media request")
            return

        try:
            BotMediaRequestManager.set_media_request_playing(oldest_enqueued_media_request)
            self.audio_output_manager.start_playing_audio_media_request(oldest_enqueued_media_request)
        except Exception as e:
            logger.info(f"Error sending raw audio: {e}")
            BotMediaRequestManager.set_media_request_failed_to_play(oldest_enqueued_media_request)

    def take_action_based_on_image_media_requests_in_db(self):
        media_type = BotMediaRequestMediaTypes.IMAGE

        # Get all enqueued image media requests for this bot, ordered by creation time
        enqueued_requests = self.bot_in_db.media_requests.filter(state=BotMediaRequestStates.ENQUEUED, media_type=media_type).order_by("created_at")

        if not enqueued_requests.exists():
            return

        # Get the most recently created request
        most_recent_request = enqueued_requests.last()

        # Mark the most recent request as FINISHED
        try:
            BotMediaRequestManager.set_media_request_playing(most_recent_request)
            self.adapter.send_raw_image(most_recent_request.media_blob.blob)
            BotMediaRequestManager.set_media_request_finished(most_recent_request)
        except Exception as e:
            logger.info(f"Error sending raw image: {e}")
            BotMediaRequestManager.set_media_request_failed_to_play(most_recent_request)

        # Mark all other enqueued requests as DROPPED
        for request in enqueued_requests.exclude(id=most_recent_request.id):
            BotMediaRequestManager.set_media_request_dropped(request)

    def take_action_based_on_video_media_requests_in_db(self):
        media_type = BotMediaRequestMediaTypes.VIDEO
        oldest_enqueued_media_request = self.bot_in_db.media_requests.filter(state=BotMediaRequestStates.ENQUEUED, media_type=media_type).order_by("created_at").first()
        if not oldest_enqueued_media_request:
            return
        currently_playing_media_request = self.bot_in_db.media_requests.filter(state=BotMediaRequestStates.PLAYING, media_type=media_type).first()
        if currently_playing_media_request:
            logger.info(f"Currently playing video media request {currently_playing_media_request.id} so cannot play another video media request")
            return

        try:
            BotMediaRequestManager.set_media_request_playing(oldest_enqueued_media_request)
            self.video_output_manager.start_playing_video_media_request(oldest_enqueued_media_request)
        except Exception as e:
            logger.info(f"Error playing video media request: {e}")
            BotMediaRequestManager.set_media_request_failed_to_play(oldest_enqueued_media_request)

    def take_action_based_on_chat_message_requests_in_db(self):
        if not self.adapter.is_ready_to_send_chat_messages():
            logger.info("Bot adapter is not ready to send chat messages, so not sending chat message requests")
            return

        chat_message_requests = self.bot_in_db.chat_message_requests.filter(state=BotChatMessageRequestStates.ENQUEUED)
        for chat_message_request in chat_message_requests:
            self.adapter.send_chat_message(text=chat_message_request.message, to_user_uuid=chat_message_request.to_user_uuid)
            BotChatMessageRequestManager.set_chat_message_request_sent(chat_message_request)

    def take_action_based_on_voice_agent_settings_in_db(self):
        if self.bot_in_db.should_launch_webpage_streamer():
            self.webpage_streamer_manager.update(url=self.bot_in_db.voice_agent_url(), output_destination=self.bot_in_db.voice_agent_video_output_destination())
        else:
            logger.info("Bot should not launch webpage streamer, so not starting webpage streamer manager")

    def take_action_based_on_media_requests_in_db(self):
        self.take_action_based_on_audio_media_requests_in_db()
        self.take_action_based_on_image_media_requests_in_db()
        self.take_action_based_on_video_media_requests_in_db()

    def take_action_based_on_transcription_settings_in_db(self):
        # If it is not a teams bot, do nothing
        meeting_type = meeting_type_from_url(self.bot_in_db.meeting_url)
        if meeting_type != MeetingTypes.TEAMS:
            logger.info(f"Bot {self.bot_in_db.object_id} is not a teams bot, so cannot update closed captions language")
            return

        # If it not using closed caption from platform, do nothing
        if self.get_recording_transcription_provider() != TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM:
            logger.info(f"Bot {self.bot_in_db.object_id} is not using closed caption from platform, so cannot update closed captions language")
            return

        # If it is a teams bot using closed caption from platform, we need to update the transcription settings
        self.adapter.update_closed_captions_language(self.bot_in_db.transcription_settings.teams_closed_captions_language())

    def handle_glib_shutdown(self):
        logger.info("handle_glib_shutdown called")

        try:
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.FATAL_ERROR,
                event_sub_type=BotEventSubTypes.FATAL_ERROR_PROCESS_TERMINATED,
            )
        except Exception as e:
            logger.info(f"Error creating FATAL_ERROR event: {e}")

        self.cleanup()
        return False

    def handle_redis_message(self, message):
        if message and message["type"] == "message":
            data = json.loads(message["data"].decode("utf-8"))
            command = data.get("command")

            if command == "sync":
                logger.info(f"Syncing bot {self.bot_in_db.object_id}")
                self.bot_in_db.refresh_from_db()
                self.take_action_based_on_bot_in_db()
            elif command == "sync_media_requests":
                logger.info(f"Syncing media requests for bot {self.bot_in_db.object_id}")
                self.bot_in_db.refresh_from_db()
                self.take_action_based_on_media_requests_in_db()
            elif command == "sync_voice_agent_settings":
                logger.info(f"Syncing voice agent settings for bot {self.bot_in_db.object_id}")
                self.bot_in_db.refresh_from_db()
                self.take_action_based_on_voice_agent_settings_in_db()
            elif command == "sync_transcription_settings":
                logger.info(f"Syncing transcription settings for bot {self.bot_in_db.object_id}")
                self.bot_in_db.refresh_from_db()
                self.take_action_based_on_transcription_settings_in_db()
            elif command == "sync_chat_message_requests":
                logger.info(f"Syncing chat message requests for bot {self.bot_in_db.object_id}")
                self.bot_in_db.refresh_from_db()
                self.take_action_based_on_chat_message_requests_in_db()
            elif command == "pause_recording":
                logger.info(f"Pausing recording for bot {self.bot_in_db.object_id}")
                self.bot_in_db.refresh_from_db()
                self.pause_recording()
            elif command == "resume_recording":
                logger.info(f"Resuming recording for bot {self.bot_in_db.object_id}")
                self.bot_in_db.refresh_from_db()
                self.resume_recording()
            elif command == "admit_from_waiting_room":
                logger.info(f"Admitting from waiting room for bot {self.bot_in_db.object_id}")
                self.bot_in_db.refresh_from_db()
                self.admit_from_waiting_room()
            elif command == "change_gallery_view_page_next" or command == "change_gallery_view_page_previous":
                logger.info(f"Changing gallery view page for bot {self.bot_in_db.object_id}. Command: {command}")
                self.bot_in_db.refresh_from_db()
                self.change_gallery_view_page(next_page=(command == "change_gallery_view_page_next"))
            else:
                logger.info(f"Unknown command: {command}")

    def admit_from_waiting_room(self):
        if not BotEventManager.is_state_that_can_admit_from_waiting_room(self.bot_in_db.state):
            logger.info(f"Bot {self.bot_in_db.object_id} is in state {BotStates.state_to_api_code(self.bot_in_db.state)} and cannot admit from waiting room")
            return
        self.adapter.admit_from_waiting_room()

    def change_gallery_view_page(self, next_page: bool):
        if not BotEventManager.is_state_that_can_change_gallery_view_page(self.bot_in_db.state):
            logger.info(f"Bot {self.bot_in_db.object_id} is in state {BotStates.state_to_api_code(self.bot_in_db.state)} and cannot change gallery view pagination")
            return
        self.adapter.change_gallery_view_page(next_page)

    def pause_recording_for_pipeline_objects(self):
        pause_recording_success = self.screen_and_audio_recorder.pause_recording() if self.screen_and_audio_recorder else True
        if not pause_recording_success:
            return False
        if self.gstreamer_pipeline:
            self.gstreamer_pipeline.pause_recording()
        self.adapter.pause_recording()
        return True

    def pause_recording_for_pipeline_objects_raise_on_failure(self):
        pause_recording_for_pipeline_objects_success = self.pause_recording_for_pipeline_objects()
        if not pause_recording_for_pipeline_objects_success:
            raise Exception(f"Failed to pause recording for bot {self.bot_in_db.object_id}")

    def pause_recording(self):
        if not BotEventManager.is_state_that_can_pause_recording(self.bot_in_db.state):
            logger.info(f"Bot {self.bot_in_db.object_id} is in state {BotStates.state_to_api_code(self.bot_in_db.state)} and cannot pause recording")
            return
        pause_recording_for_pipeline_objects_success = self.pause_recording_for_pipeline_objects()
        if not pause_recording_for_pipeline_objects_success:
            logger.error(f"Failed to pause recording for bot {self.bot_in_db.object_id}")
            return
        BotEventManager.create_event(
            bot=self.bot_in_db,
            event_type=BotEventTypes.RECORDING_PAUSED,
        )

    def start_or_resume_recording_for_pipeline_objects(self):
        resume_recording_success = self.screen_and_audio_recorder.resume_recording() if self.screen_and_audio_recorder else True
        if not resume_recording_success:
            logger.error(f"Failed to resume recording for bot {self.bot_in_db.object_id}")
            return False
        if self.gstreamer_pipeline:
            self.gstreamer_pipeline.resume_recording()
        self.adapter.start_or_resume_recording()
        return True

    def start_or_resume_recording_for_pipeline_objects_raise_on_failure(self):
        start_or_resume_recording_for_pipeline_objects_success = self.start_or_resume_recording_for_pipeline_objects()
        if not start_or_resume_recording_for_pipeline_objects_success:
            raise Exception(f"Failed to resume recording for bot {self.bot_in_db.object_id}")

    def resume_recording(self):
        if not BotEventManager.is_state_that_can_resume_recording(self.bot_in_db.state):
            logger.info(f"Bot {self.bot_in_db.object_id} is in state {BotStates.state_to_api_code(self.bot_in_db.state)} and cannot resume recording")
            return
        start_or_resume_recording_for_pipeline_objects_success = self.start_or_resume_recording_for_pipeline_objects()
        if not start_or_resume_recording_for_pipeline_objects_success:
            logger.error(f"Failed to resume recording for bot {self.bot_in_db.object_id}")
            return
        BotEventManager.create_event(
            bot=self.bot_in_db,
            event_type=BotEventTypes.RECORDING_RESUMED,
        )

    def set_bot_heartbeat(self):
        if self.bot_in_db.last_heartbeat_timestamp is None or self.bot_in_db.last_heartbeat_timestamp <= int(timezone.now().timestamp()) - 60:
            self.bot_in_db.set_heartbeat()

    def on_main_loop_timeout(self):
        try:
            if self.first_timeout_call:
                logger.info("First timeout call - taking initial action")
                self.bot_in_db.refresh_from_db()
                self.take_action_based_on_bot_in_db()
                self.first_timeout_call = False

            # Set heartbeat
            self.set_bot_heartbeat()

            # Process audio chunks
            self.per_participant_non_streaming_audio_input_manager.process_chunks()

            # Monitor transcription
            self.per_participant_streaming_audio_input_manager.monitor_transcription()

            # Process captions
            self.closed_caption_manager.process_captions()

            # Check if auto-leave conditions are met
            self.adapter.check_auto_leave_conditions()

            # Process audio output
            self.audio_output_manager.monitor_currently_playing_audio_media_request()

            # Process video output
            self.video_output_manager.monitor_currently_playing_video_media_request()

            # For staged bots, check if its time to join
            self.join_if_staged_and_time_to_join()

            # Take a resource snapshot if needed
            self.bot_resource_snapshot_taker.save_snapshot_if_needed()

            return True

        except Exception as e:
            logger.info(f"Error in timeout callback: {e}")
            logger.info("Traceback:")
            logger.info(traceback.format_exc())
            self.handle_exception_in_timeout_callback(e)
            return False

    def handle_exception_in_timeout_callback(self, e):
        try:
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.FATAL_ERROR,
                event_sub_type=BotEventSubTypes.FATAL_ERROR_ATTENDEE_INTERNAL_ERROR,
                event_metadata={"error": str(e)},
            )
        except Exception as e:
            logger.info(f"Error in handle_exception_in_timeout_callback: {e}")
            logger.info("Traceback:")
            logger.info(traceback.format_exc())
        self.cleanup()

    def get_recording_in_progress(self):
        return RecordingManager.get_recording_in_progress(self.bot_in_db)

    def save_closed_caption_utterance(self, message):
        participant, _ = Participant.objects.get_or_create(
            bot=self.bot_in_db,
            uuid=message["participant_uuid"],
            defaults={
                "user_uuid": message["participant_user_uuid"],
                "full_name": message["participant_full_name"],
                "is_the_bot": message["participant_is_the_bot"],
                "is_host": message["participant_is_host"],
            },
        )

        # Create new utterance record
        recording_in_progress = self.get_recording_in_progress()
        if recording_in_progress is None:
            logger.warning(f"Warning: No recording in progress found so cannot save closed caption utterance. Message: {message}")
            return
        source_uuid = f"{recording_in_progress.object_id}-{message['source_uuid_suffix']}"
        utterance, _ = Utterance.objects.update_or_create(
            recording=recording_in_progress,
            source_uuid=source_uuid,
            defaults={
                "source": Utterance.Sources.CLOSED_CAPTION_FROM_PLATFORM,
                "participant": participant,
                "transcription": {"transcript": message["text"]},
                "timestamp_ms": message["timestamp_ms"],
                "duration_ms": message["duration_ms"],
                "sample_rate": None,
            },
        )

        # Create webhook event
        trigger_webhook(
            webhook_trigger_type=WebhookTriggerTypes.TRANSCRIPT_UPDATE,
            bot=self.bot_in_db,
            payload=utterance_webhook_payload(utterance),
        )

        RecordingManager.set_recording_transcription_in_progress(recording_in_progress)

    def process_individual_audio_chunk(self, message):
        from bots.tasks.process_utterance_task import process_utterance

        logger.info("Received message that new individual audio chunk was detected")

        # Create participant record if it doesn't exist
        participant, _ = Participant.objects.get_or_create(
            bot=self.bot_in_db,
            uuid=message["participant_uuid"],
            defaults={
                "user_uuid": message["participant_user_uuid"],
                "full_name": message["participant_full_name"],
                "is_the_bot": message["participant_is_the_bot"],
                "is_host": message["participant_is_host"],
            },
        )

        recording_in_progress = self.get_recording_in_progress()
        if recording_in_progress is None:
            logger.warning("Warning: No recording in progress found so cannot save individual audio utterance.")
            return

        audio_chunk = AudioChunk.objects.create(
            recording=recording_in_progress,
            audio_blob=message["audio_data"],
            audio_format=AudioChunk.AudioFormat.PCM,
            timestamp_ms=message["timestamp_ms"] - self.get_per_participant_audio_utterance_delay_ms(),
            duration_ms=len(message["audio_data"]) / ((message["sample_rate"] / 1000) * 2),
            sample_rate=message["sample_rate"],
            source=AudioChunk.Sources.PER_PARTICIPANT_AUDIO,
            participant=participant,
        )

        if not self.save_utterances_for_individual_audio_chunks():
            return

        # Create new utterance record
        utterance = Utterance.objects.create(
            source=Utterance.Sources.PER_PARTICIPANT_AUDIO,
            async_transcription=None,  # This utterance is created during the meeting, so it's not associated with an async transcription
            recording=recording_in_progress,
            participant=participant,
            audio_chunk=audio_chunk,
            timestamp_ms=audio_chunk.timestamp_ms,
            duration_ms=audio_chunk.duration_ms,
        )

        # Set the recording transcription in progress
        RecordingManager.set_recording_transcription_in_progress(recording_in_progress)

        # Process the utterance immediately
        process_utterance.delay(utterance.id)
        return

    def on_new_chat_message(self, chat_message):
        GLib.idle_add(lambda: self.upsert_chat_message(chat_message))

    def add_participant_event(self, event):
        logger.info(f"Adding participant event: {event}")

        participant = self.adapter.get_participant(event["participant_uuid"])

        if participant is None:
            logger.warning(f"Warning: No participant found for participant event: {event}")
            return

        # Create participant record if it doesn't exist
        participant, _ = Participant.objects.get_or_create(
            bot=self.bot_in_db,
            uuid=participant["participant_uuid"],
            defaults={
                "user_uuid": participant["participant_user_uuid"],
                "full_name": participant["participant_full_name"],
                "is_the_bot": participant["participant_is_the_bot"],
                "is_host": participant["participant_is_host"],
            },
        )

        if event["event_type"] == ParticipantEventTypes.UPDATE:
            if "isHost" in event["event_data"]:
                participant.is_host = event["event_data"]["isHost"]["after"]
                participant.save()
                logger.info(f"Updated participant {participant.object_id} is host to {participant.is_host}")
            # Don't save this event type in the database for now.
            return

        participant_event = ParticipantEvent.objects.create(
            participant=participant,
            event_type=event["event_type"],
            event_data=event["event_data"],
            timestamp_ms=event["timestamp_ms"],
        )

        # Don't send webhook for the bot itself
        if participant.is_the_bot:
            return

        # Don't send webhook for non join / leave events
        if participant_event.event_type != ParticipantEventTypes.JOIN and participant_event.event_type != ParticipantEventTypes.LEAVE:
            return

        trigger_webhook(
            webhook_trigger_type=WebhookTriggerTypes.PARTICIPANT_EVENTS_JOIN_LEAVE,
            bot=self.bot_in_db,
            payload=participant_event_webhook_payload(participant_event),
        )

        return

    def upsert_chat_message(self, chat_message):
        logger.info(f"Upserting chat message: {chat_message}")

        participant = self.adapter.get_participant(chat_message["participant_uuid"])

        if participant is None:
            logger.warning(f"Warning: No participant found for chat message: {chat_message}")
            return

        participant, _ = Participant.objects.get_or_create(
            bot=self.bot_in_db,
            uuid=participant["participant_uuid"],
            defaults={
                "user_uuid": participant["participant_user_uuid"],
                "full_name": participant["participant_full_name"],
                "is_the_bot": participant["participant_is_the_bot"],
                "is_host": participant["participant_is_host"],
            },
        )

        recording_in_progress = self.get_recording_in_progress()
        if recording_in_progress is None:
            logger.warning(f"Warning: No recording in progress found so cannot save chat message. Message: {chat_message}")
            return

        chat_message_in_db, _ = ChatMessage.objects.update_or_create(
            bot=self.bot_in_db,
            source_uuid=f"{recording_in_progress.object_id}-{chat_message['message_uuid']}",
            defaults={
                "timestamp": chat_message["timestamp"],
                "to": ChatMessageToOptions.ONLY_BOT if chat_message.get("to_bot") else ChatMessageToOptions.EVERYONE,
                "text": chat_message["text"],
                "participant": participant,
                "additional_data": chat_message.get("additional_data", {}),
            },
        )

        # Create webhook event
        trigger_webhook(
            webhook_trigger_type=WebhookTriggerTypes.CHAT_MESSAGES_UPDATE,
            bot=self.bot_in_db,
            payload=chat_message_webhook_payload(chat_message_in_db),
        )

        return

    def on_message_from_adapter(self, message):
        GLib.idle_add(lambda: self.take_action_based_on_message_from_adapter(message))

    def flush_utterances(self):
        if self.per_participant_non_streaming_audio_input_manager:
            logger.info("Flushing utterances...")
            self.per_participant_non_streaming_audio_input_manager.flush_utterances()
        if self.closed_caption_manager:
            logger.info("Flushing captions...")
            self.closed_caption_manager.flush_captions()

    def save_debug_recording(self):
        # Only save if the file exists
        if not os.path.exists(BotAdapter.DEBUG_RECORDING_FILE_PATH):
            logger.info(f"Debug recording file at {BotAdapter.DEBUG_RECORDING_FILE_PATH} does not exist, not saving")
            return

        # Find the bot's last event
        last_bot_event = self.bot_in_db.last_bot_event()
        if last_bot_event:
            debug_screenshot = BotDebugScreenshot.objects.create(bot_event=last_bot_event)

            # Save the file directly from the file path
            with open(BotAdapter.DEBUG_RECORDING_FILE_PATH, "rb") as f:
                debug_screenshot.file.save(f"debug_screen_recording_{debug_screenshot.object_id}.mp4", f, save=True)
            logger.info(f"Saved debug recording with ID {debug_screenshot.object_id}")

    def on_message_from_websocket_audio(self, message_json: str):
        try:
            message = json.loads(message_json)
            if message["trigger"] == RealtimeTriggerTypes.type_to_api_code(RealtimeTriggerTypes.BOT_OUTPUT_AUDIO_CHUNK):
                chunk = b64decode(message["data"]["chunk"])
                sample_rate = message["data"]["sample_rate"]
                self.realtime_audio_output_manager.add_chunk(chunk, sample_rate)
            else:
                if not hasattr(self, "websocket_audio_error_ticker"):
                    self.websocket_audio_error_ticker = 0

                if self.websocket_audio_error_ticker % 1000 == 0:
                    logger.error("Received unknown message from websocket: %s", message)
                self.websocket_audio_error_ticker += 1
        except Exception as e:
            # Set the ticker to zero if its not an attribute
            if not hasattr(self, "websocket_audio_error_ticker"):
                self.websocket_audio_error_ticker = 0

            if self.websocket_audio_error_ticker % 1000 == 0:
                logger.error(f"Error processing message from websocket: {e}")
            self.websocket_audio_error_ticker += 1

    def save_debug_artifacts(self, message, new_bot_event):
        screenshot_available = message.get("screenshot_path") is not None
        mhtml_file_available = message.get("mhtml_file_path") is not None

        if screenshot_available:
            # Create debug screenshot
            debug_screenshot = BotDebugScreenshot.objects.create(bot_event=new_bot_event)

            # Read the file content from the path
            with open(message.get("screenshot_path"), "rb") as f:
                screenshot_content = f.read()
                debug_screenshot.file.save(
                    f"debug_screenshot_{debug_screenshot.object_id}.png",
                    ContentFile(screenshot_content),
                    save=True,
                )

        if mhtml_file_available:
            # Create debug screenshot
            mhtml_debug_screenshot = BotDebugScreenshot.objects.create(bot_event=new_bot_event)

            with open(message.get("mhtml_file_path"), "rb") as f:
                mhtml_content = f.read()
                mhtml_debug_screenshot.file.save(
                    f"debug_screenshot_{mhtml_debug_screenshot.object_id}.mhtml",
                    ContentFile(mhtml_content),
                    save=True,
                )

    def take_action_based_on_message_from_adapter(self, message):
        if message.get("message") == BotAdapter.Messages.JOINING_BREAKOUT_ROOM:
            logger.info("Received message that bot is joining breakout room")
            BotEventManager.create_event(bot=self.bot_in_db, event_type=BotEventTypes.BOT_BEGAN_JOINING_BREAKOUT_ROOM)
            return

        if message.get("message") == BotAdapter.Messages.LEAVING_BREAKOUT_ROOM:
            logger.info("Received message that bot is leaving breakout room")
            BotEventManager.create_event(bot=self.bot_in_db, event_type=BotEventTypes.BOT_BEGAN_LEAVING_BREAKOUT_ROOM)
            return

        if message.get("message") == BotAdapter.Messages.REQUEST_TO_JOIN_DENIED:
            logger.info("Received message that request to join was denied")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_REQUEST_TO_JOIN_DENIED,
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.COULD_NOT_CONNECT_TO_MEETING:
            logger.info("Received message that could not connect to meeting")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_UNABLE_TO_CONNECT_TO_MEETING,
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.MEETING_NOT_FOUND:
            logger.info("Received message that meeting not found")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_MEETING_NOT_FOUND,
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.LOGIN_REQUIRED:
            logger.info("Received message that login required")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_LOGIN_REQUIRED,
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.LOGIN_ATTEMPT_FAILED:
            logger.info("Received message that login attempt failed")
            new_bot_event = BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_BOT_LOGIN_ATTEMPT_FAILED,
            )

            self.save_debug_artifacts(message, new_bot_event)

            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.BLOCKED_BY_PLATFORM_REPEATEDLY:
            from bots.tasks.restart_bot_pod_task import restart_bot_pod

            bot_start_time = self.bot_in_db.join_at or self.bot_in_db.created_at
            if bot_start_time < timezone.now() - timedelta(minutes=15):
                logger.info("Received message that we were blocked by platform repeatedly but bot was created more than 15 minutes ago, so not recreating pod")

                new_bot_event = BotEventManager.create_event(
                    bot=self.bot_in_db,
                    event_type=BotEventTypes.FATAL_ERROR,
                    event_sub_type=BotEventSubTypes.FATAL_ERROR_UI_ELEMENT_NOT_FOUND,
                    event_metadata={
                        "bot_restarts_exceeded_max_retries": True,
                    },
                )
                self.cleanup()
                return

            logger.info("Received message that we were blocked by platform repeatedly, so recreating pod")
            # Run task to restart the bot pod with 1 minute delay
            restart_bot_pod.apply_async(args=[self.bot_in_db.id], countdown=60)
            # Don't do the normal cleanup tasks because we'll be restarting the pod
            if self.main_loop and self.main_loop.is_running():
                logger.info("Quitting main loop")
                self.main_loop.quit()
            return

        if message.get("message") == BotAdapter.Messages.UI_ELEMENT_NOT_FOUND:
            logger.info(f"Received message that UI element not found at {message.get('current_time')}")

            screenshot_available = message.get("screenshot_path") is not None
            mhtml_file_available = message.get("mhtml_file_path") is not None

            new_bot_event = BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.FATAL_ERROR,
                event_sub_type=BotEventSubTypes.FATAL_ERROR_UI_ELEMENT_NOT_FOUND,
                event_metadata={
                    "step": message.get("step"),
                    "current_time": message.get("current_time").isoformat(),
                    "exception_type": message.get("exception_type"),
                    "inner_exception_type": message.get("inner_exception_type"),
                },
            )

            logger.info(f"Created bot event for #{self.bot_in_db.object_id} for UI element not found. Exception info: {message}")

            if screenshot_available:
                # Create debug screenshot
                debug_screenshot = BotDebugScreenshot.objects.create(bot_event=new_bot_event)

                # Read the file content from the path
                with open(message.get("screenshot_path"), "rb") as f:
                    screenshot_content = f.read()
                    debug_screenshot.file.save(
                        f"debug_screenshot_{debug_screenshot.object_id}.png",
                        ContentFile(screenshot_content),
                        save=True,
                    )

            if mhtml_file_available:
                # Create debug screenshot
                mhtml_debug_screenshot = BotDebugScreenshot.objects.create(bot_event=new_bot_event)

                with open(message.get("mhtml_file_path"), "rb") as f:
                    mhtml_content = f.read()
                    mhtml_debug_screenshot.file.save(
                        f"debug_screenshot_{mhtml_debug_screenshot.object_id}.mhtml",
                        ContentFile(mhtml_content),
                        save=True,
                    )

            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.ADAPTER_REQUESTED_BOT_LEAVE_MEETING:
            logger.info(f"Received message that adapter requested bot leave meeting reason={message.get('leave_reason')}")

            event_sub_type_for_reason = {
                BotAdapter.LEAVE_REASON.AUTO_LEAVE_SILENCE: BotEventSubTypes.LEAVE_REQUESTED_AUTO_LEAVE_SILENCE,
                BotAdapter.LEAVE_REASON.AUTO_LEAVE_ONLY_PARTICIPANT_IN_MEETING: BotEventSubTypes.LEAVE_REQUESTED_AUTO_LEAVE_ONLY_PARTICIPANT_IN_MEETING,
                BotAdapter.LEAVE_REASON.AUTO_LEAVE_MAX_UPTIME: BotEventSubTypes.LEAVE_REQUESTED_AUTO_LEAVE_MAX_UPTIME_EXCEEDED,
            }[message.get("leave_reason")]

            BotEventManager.create_event(bot=self.bot_in_db, event_type=BotEventTypes.LEAVE_REQUESTED, event_sub_type=event_sub_type_for_reason)
            BotEventManager.set_requested_bot_action_taken_at(self.bot_in_db)
            self.adapter.leave()
            return

        if message.get("message") == BotAdapter.Messages.MEETING_ENDED:
            logger.info("Received message that meeting ended")
            self.flush_utterances()
            if self.bot_in_db.state == BotStates.LEAVING:
                BotEventManager.create_event(bot=self.bot_in_db, event_type=BotEventTypes.BOT_LEFT_MEETING)
            else:
                BotEventManager.create_event(bot=self.bot_in_db, event_type=BotEventTypes.MEETING_ENDED)
            self.cleanup()

            return

        if message.get("message") == BotAdapter.Messages.ZOOM_MEETING_STATUS_FAILED_UNABLE_TO_JOIN_EXTERNAL_MEETING:
            logger.info(f"Received message that meeting status failed unable to join external meeting with zoom_result_code={message.get('zoom_result_code')}")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_UNPUBLISHED_ZOOM_APP,
                event_metadata={"zoom_result_code": str(message.get("zoom_result_code"))},
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.ZOOM_MEETING_STATUS_FAILED:
            logger.info(f"Received message that meeting status failed with zoom_result_code={message.get('zoom_result_code')}")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_ZOOM_MEETING_STATUS_FAILED,
                event_metadata={"zoom_result_code": str(message.get("zoom_result_code"))},
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.ZOOM_AUTHORIZATION_FAILED:
            logger.info(f"Received message that authorization failed with zoom_result_code={message.get('zoom_result_code')}")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_ZOOM_AUTHORIZATION_FAILED,
                event_metadata={"zoom_result_code": str(message.get("zoom_result_code"))},
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.ZOOM_SDK_INTERNAL_ERROR:
            logger.info(f"Received message that SDK internal error with zoom_result_code={message.get('zoom_result_code')}")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_ZOOM_SDK_INTERNAL_ERROR,
                event_metadata={"zoom_result_code": str(message.get("zoom_result_code"))},
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.LEAVE_MEETING_WAITING_ROOM_TIMEOUT_EXCEEDED:
            logger.info("Received message to leave meeting because waiting room timeout exceeded")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_WAITING_ROOM_TIMEOUT_EXCEEDED,
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.LEAVE_MEETING_WAITING_FOR_HOST:
            logger.info("Received message to Leave meeting because received waiting for host status")
            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.COULD_NOT_JOIN,
                event_sub_type=BotEventSubTypes.COULD_NOT_JOIN_MEETING_NOT_STARTED_WAITING_FOR_HOST,
            )
            self.cleanup()
            return

        if message.get("message") == BotAdapter.Messages.BOT_PUT_IN_WAITING_ROOM:
            logger.info("Received message to put bot in waiting room")
            BotEventManager.create_event(bot=self.bot_in_db, event_type=BotEventTypes.BOT_PUT_IN_WAITING_ROOM)
            return

        if message.get("message") == BotAdapter.Messages.BOT_JOINED_MEETING:
            if self.bot_in_db.state == BotStates.JOINING_BREAKOUT_ROOM:
                logger.info("Received message that bot joined breakout room")
                BotEventManager.create_event(bot=self.bot_in_db, event_type=BotEventTypes.BOT_JOINED_BREAKOUT_ROOM)
                return

            if self.bot_in_db.state == BotStates.LEAVING_BREAKOUT_ROOM:
                logger.info("Received message that bot left breakout room")
                BotEventManager.create_event(bot=self.bot_in_db, event_type=BotEventTypes.BOT_LEFT_BREAKOUT_ROOM)
                return

            logger.info("Received message that bot joined meeting")
            BotEventManager.create_event(bot=self.bot_in_db, event_type=BotEventTypes.BOT_JOINED_MEETING)
            return

        if message.get("message") == BotAdapter.Messages.READY_TO_SEND_CHAT_MESSAGE:
            logger.info("Received message that bot is ready to send chat message")
            self.take_action_based_on_chat_message_requests_in_db()
            return

        if message.get("message") == BotAdapter.Messages.READY_TO_SHOW_BOT_IMAGE:
            logger.info("Received message that bot is ready to show image")
            # If there are any image media requests, this will start playing them
            # For now the only type of media request is an image, so this will start showing the bot's image
            self.take_action_based_on_image_media_requests_in_db()
            return

        if message.get("message") == BotAdapter.Messages.READY_TO_SHOW_WEBPAGE_STREAM:
            logger.info("Received message that bot is ready to show webpage stream")
            # If there are any webpage stream media requests, this will start playing them
            # For now the only type of media request is a webpage stream, so this will start showing the bot's webpage stream
            self.take_action_based_on_voice_agent_settings_in_db()
            return

        if message.get("message") == BotAdapter.Messages.BOT_RECORDING_PERMISSION_GRANTED:
            logger.info("Received message that bot recording permission granted")

            # The internal pipeline needs to start or resume recording.
            self.start_or_resume_recording_for_pipeline_objects_raise_on_failure()

            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.BOT_RECORDING_PERMISSION_GRANTED,
            )
            return

        if message.get("message") == BotAdapter.Messages.BOT_RECORDING_PERMISSION_DENIED:
            logger.info("Received message that bot recording permission denied")

            # The internal pipeline needs to stop recording.
            self.pause_recording_for_pipeline_objects_raise_on_failure()

            if message.get("denied_reason") == BotAdapter.BOT_RECORDING_PERMISSION_DENIED_REASON.HOST_DENIED_PERMISSION:
                event_sub_type_for_permission_denied = BotEventSubTypes.BOT_RECORDING_PERMISSION_DENIED_HOST_DENIED_PERMISSION
            elif message.get("denied_reason") == BotAdapter.BOT_RECORDING_PERMISSION_DENIED_REASON.REQUEST_TIMED_OUT:
                event_sub_type_for_permission_denied = BotEventSubTypes.BOT_RECORDING_PERMISSION_DENIED_REQUEST_TIMED_OUT
            elif message.get("denied_reason") == BotAdapter.BOT_RECORDING_PERMISSION_DENIED_REASON.HOST_CLIENT_CANNOT_GRANT_PERMISSION:
                event_sub_type_for_permission_denied = BotEventSubTypes.BOT_RECORDING_PERMISSION_DENIED_HOST_CLIENT_CANNOT_GRANT_PERMISSION
            else:
                raise Exception(f"Received unexpected denied reason from bot adapter: {message.get('denied_reason')}")

            BotEventManager.create_event(
                bot=self.bot_in_db,
                event_type=BotEventTypes.BOT_RECORDING_PERMISSION_DENIED,
                event_sub_type=event_sub_type_for_permission_denied,
            )
            return

        raise Exception(f"Received unexpected message from bot adapter: {message}")
