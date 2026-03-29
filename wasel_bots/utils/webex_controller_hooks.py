from bots.models import Credentials

class _WebexCredentialsWrapper:
    """
    Wraps a Credentials instance (credential_type=WEBEX) to expose the same
    interface as the former WebexBotConnection model for WebexBotAdapter.
    """
    def __init__(self, credentials_record):
        self._credentials = credentials_record
        self.state = 1  # CONNECTED
        self.connection_failure_data = None

    @property
    def object_id(self):
        return str(self._credentials.id)

    def get_credentials(self):
        return self._credentials.get_credentials()

    def set_credentials(self, credentials_dict):
        self._credentials.set_credentials(credentials_dict)

    @property
    def client_id(self):
        creds = self.get_credentials()
        return creds.get("client_id") if creds else None

    @property
    def client_secret(self):
        creds = self.get_credentials()
        return creds.get("client_secret") if creds else None

    @property
    def refresh_token(self):
        creds = self.get_credentials()
        return creds.get("refresh_token") if creds else None

    @property
    def access_token(self):
        creds = self.get_credentials()
        return creds.get("access_token") if creds else None

    @property
    def expires_at(self):
        creds = self.get_credentials()
        return creds.get("expires_at") if creds else None

    def save(self):
        pass


def get_webex_bot_connection(bot_controller):
    webex_credential = Credentials.objects.filter(
        project=bot_controller.bot_in_db.project,
        credential_type=13, # Credentials.CredentialTypes.WEBEX
    ).first()

    if not webex_credential:
        raise Exception(
            "Webex credentials not found for this project. "
            "Please configure Webex credentials (Client ID, Client Secret, Refresh Token) in project settings."
        )

    return _WebexCredentialsWrapper(webex_credential)


def get_webex_bot_adapter(bot_controller):
    from wasel_bots.utils.meeting_url_utils import parse_webex_join_url
    from wasel_bots.adapters.webex_bot_adapter.webex_bot_adapter import WebexBotAdapter

    if bot_controller.should_capture_audio_chunks():
        add_audio_chunk_callback = bot_controller.per_participant_audio_input_manager().add_chunk
    else:
        add_audio_chunk_callback = None

    webex_bot_connection = get_webex_bot_connection(bot_controller)
    meeting_info = parse_webex_join_url(bot_controller.bot_in_db.meeting_url)

    return WebexBotAdapter(
        webex_bot_connection=webex_bot_connection,
        webex_meeting_url=meeting_info['meeting_url'],
        webex_meeting_password=meeting_info.get('password'),
        meeting_url=bot_controller.bot_in_db.meeting_url,
        display_name=bot_controller.bot_in_db.name,
        send_message_callback=bot_controller.on_message_from_adapter,
        add_audio_chunk_callback=add_audio_chunk_callback,
        add_video_frame_callback=None,
        wants_any_video_frames_callback=None,
        add_mixed_audio_chunk_callback=bot_controller.add_mixed_audio_chunk_callback if bot_controller.pipeline_configuration.websocket_stream_audio else None,
        upsert_caption_callback=bot_controller.closed_caption_manager.upsert_caption if bot_controller.save_utterances_for_closed_captions() else None,
        upsert_chat_message_callback=bot_controller.on_new_chat_message,
        add_participant_event_callback=bot_controller.add_participant_event,
        automatic_leave_configuration=bot_controller.automatic_leave_configuration,
        add_encoded_mp4_chunk_callback=None,
        recording_view=bot_controller.bot_in_db.recording_view(),
        enable_transcription=True,
        should_create_debug_recording=bot_controller.bot_in_db.create_debug_recording(),
        start_recording_screen_callback=bot_controller.screen_and_audio_recorder.start_recording if bot_controller.screen_and_audio_recorder else None,
        stop_recording_screen_callback=bot_controller.screen_and_audio_recorder.stop_recording if bot_controller.screen_and_audio_recorder else None,
        video_frame_size=bot_controller.bot_in_db.recording_dimensions(),
        record_chat_messages_when_paused=bot_controller.bot_in_db.record_chat_messages_when_paused(),
        disable_incoming_video=bot_controller.disable_incoming_video_for_web_bots(),
        record_participant_speech_start_stop_events=bot_controller.bot_in_db.record_participant_speech_start_stop_events(),
    )