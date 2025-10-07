import base64
import json
from datetime import datetime, timezone

from django.test import Client, TransactionTestCase
from rest_framework import status

from accounts.models import Organization
from bots.models import (
    ApiKey,
    AsyncTranscription,
    AsyncTranscriptionStates,
    AudioChunk,
    Bot,
    BotChatMessageRequest,
    BotChatMessageRequestStates,
    BotChatMessageToOptions,
    BotMediaRequest,
    BotMediaRequestMediaTypes,
    BotStates,
    ChatMessage,
    ChatMessageToOptions,
    MediaBlob,
    Participant,
    ParticipantEvent,
    ParticipantEventTypes,
    Project,
    Recording,
    RecordingStates,
    RecordingTypes,
    TranscriptionTypes,
    Utterance,
)


class ApiObjectAccessIntegrationTest(TransactionTestCase):
    """Integration tests for API object access control in bots_api_views.py"""

    def setUp(self):
        """Set up test environment with multiple organizations, projects, and API keys"""

        # Create two organizations
        self.organization_a = Organization.objects.create(name="Organization A", centicredits=10000, is_async_transcription_enabled=True)
        self.organization_b = Organization.objects.create(name="Organization B", centicredits=10000, is_async_transcription_enabled=True)

        # Create projects in each organization
        self.project_a = Project.objects.create(name="Project A", organization=self.organization_a)
        self.project_b = Project.objects.create(name="Project B", organization=self.organization_b)

        # Create API keys for each project
        self.api_key_a, self.api_key_a_plain = ApiKey.create(project=self.project_a, name="API Key A")
        self.api_key_b, self.api_key_b_plain = ApiKey.create(project=self.project_b, name="API Key B")

        # Create test objects for access testing
        self._create_test_objects()

        # Create test client
        self.client = Client()

    def _create_test_objects(self):
        """Create test objects (bots, recordings, etc.) for access testing"""

        # Create bots in each project
        self.bot_a = Bot.objects.create(project=self.project_a, name="Bot A", meeting_url="https://zoom.us/j/1234567890", state=BotStates.JOINED_RECORDING)
        self.bot_b = Bot.objects.create(project=self.project_b, name="Bot B", meeting_url="https://zoom.us/j/0987654321", state=BotStates.JOINED_RECORDING)

        # Create recordings for each bot
        self.recording_a = Recording.objects.create(bot=self.bot_a, recording_type=RecordingTypes.AUDIO_AND_VIDEO, transcription_type=TranscriptionTypes.NON_REALTIME, is_default_recording=True, state=RecordingStates.IN_PROGRESS)
        self.recording_b = Recording.objects.create(bot=self.bot_b, recording_type=RecordingTypes.AUDIO_AND_VIDEO, transcription_type=TranscriptionTypes.NON_REALTIME, is_default_recording=True, state=RecordingStates.IN_PROGRESS)

        # Create participants for each bot
        self.participant_a = Participant.objects.create(bot=self.bot_a, uuid="participant_a_uuid", full_name="Participant A")
        self.participant_b = Participant.objects.create(bot=self.bot_b, uuid="participant_b_uuid", full_name="Participant B")

        # Create utterances for transcript testing
        self.audio_chunk_a = AudioChunk.objects.create(recording=self.recording_a, participant=self.participant_a, audio_blob=b"dummy_audio_data", timestamp_ms=1000, duration_ms=2000, sample_rate=16000)
        self.audio_chunk_b = AudioChunk.objects.create(recording=self.recording_b, participant=self.participant_b, audio_blob=b"dummy_audio_data", timestamp_ms=1000, duration_ms=2000, sample_rate=16000)
        self.utterance_a = Utterance.objects.create(recording=self.recording_a, participant=self.participant_a, audio_chunk=self.audio_chunk_a, timestamp_ms=1000, duration_ms=2000, transcription={"transcript": "Hello from bot A"})
        self.utterance_b = Utterance.objects.create(recording=self.recording_b, participant=self.participant_b, audio_chunk=self.audio_chunk_b, timestamp_ms=1000, duration_ms=2000, transcription={"transcript": "Hello from bot B"})

        # Create chat messages
        self.chat_message_a = ChatMessage.objects.create(bot=self.bot_a, participant=self.participant_a, text="Chat message from bot A", to=ChatMessageToOptions.EVERYONE, timestamp=123456789)
        self.chat_message_b = ChatMessage.objects.create(bot=self.bot_b, participant=self.participant_b, text="Chat message from bot B", to=ChatMessageToOptions.EVERYONE, timestamp=123456789)

        # Create participant events
        self.participant_event_a = ParticipantEvent.objects.create(participant=self.participant_a, event_type=ParticipantEventTypes.JOIN, timestamp_ms=1000)
        self.participant_event_b = ParticipantEvent.objects.create(participant=self.participant_b, event_type=ParticipantEventTypes.JOIN, timestamp_ms=1000)

        self.minimal_mp3_file_base64 = "data:audio/mpeg;base64,SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU4Ljc2LjEwMAAAAAAAAAAAAAAA/+M4wAAAAAAAAAAAAEluZm8AAAAPAAAAAwAAAbAAqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV////////////////////////////////////////////AAAAAExhdmM1OC4xMwAAAAAAAAAAAAAAACQDkAAAAAAAAAGw9wrNaQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/+MYxAAAAANIAAAAAExBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV/+MYxDsAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV/+MYxHYAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"

        # Create media blobs and media requests for testing
        self.media_blob_a = MediaBlob.objects.create(project=self.project_a, blob=base64.b64decode(self.minimal_mp3_file_base64), content_type="audio/mp3", duration_ms=3000)
        self.media_blob_b = MediaBlob.objects.create(project=self.project_b, blob=base64.b64decode(self.minimal_mp3_file_base64), content_type="audio/mp3", duration_ms=3000)

        self.media_request_a = BotMediaRequest.objects.create(bot=self.bot_a, media_blob=self.media_blob_a, media_type=BotMediaRequestMediaTypes.AUDIO)
        self.media_request_b = BotMediaRequest.objects.create(bot=self.bot_b, media_blob=self.media_blob_b, media_type=BotMediaRequestMediaTypes.AUDIO)

        # Create chat message requests
        self.chat_request_a = BotChatMessageRequest.objects.create(bot=self.bot_a, to=BotChatMessageToOptions.EVERYONE, message="Test message A", state=BotChatMessageRequestStates.ENQUEUED)
        self.chat_request_b = BotChatMessageRequest.objects.create(bot=self.bot_b, to=BotChatMessageToOptions.EVERYONE, message="Test message B", state=BotChatMessageRequestStates.ENQUEUED)

        # Create async transcriptions for access control testing
        self.async_transcription_a = AsyncTranscription.objects.create(recording=self.recording_a, state=AsyncTranscriptionStates.COMPLETE)
        self.async_transcription_b = AsyncTranscription.objects.create(recording=self.recording_b, state=AsyncTranscriptionStates.COMPLETE)

        # Create ended bots for POST transcript testing (these need to be in ENDED state for async transcription creation)
        self.ended_bot_a = Bot.objects.create(project=self.project_a, name="Ended Bot A", meeting_url="https://zoom.us/j/1111111111", state=BotStates.ENDED, settings={"recording_settings": {"record_async_transcription_audio_chunks": True}})
        self.ended_bot_b = Bot.objects.create(project=self.project_b, name="Ended Bot B", meeting_url="https://zoom.us/j/2222222222", state=BotStates.ENDED, settings={"recording_settings": {"record_async_transcription_audio_chunks": True}})

        # Create recordings for ended bots
        self.ended_recording_a = Recording.objects.create(bot=self.ended_bot_a, recording_type=RecordingTypes.AUDIO_AND_VIDEO, transcription_type=TranscriptionTypes.NON_REALTIME, is_default_recording=True, state=RecordingStates.COMPLETE)
        self.ended_recording_b = Recording.objects.create(bot=self.ended_bot_b, recording_type=RecordingTypes.AUDIO_AND_VIDEO, transcription_type=TranscriptionTypes.NON_REALTIME, is_default_recording=True, state=RecordingStates.COMPLETE)

        # Create audio chunks for ended recordings
        self.ended_audio_chunk_a = AudioChunk.objects.create(recording=self.ended_recording_a, participant=self.participant_a, audio_blob=b"dummy_audio_data", timestamp_ms=1000, duration_ms=2000, sample_rate=16000)
        self.ended_audio_chunk_b = AudioChunk.objects.create(recording=self.ended_recording_b, participant=self.participant_b, audio_blob=b"dummy_audio_data", timestamp_ms=1000, duration_ms=2000, sample_rate=16000)

    def _make_authenticated_request(self, method, url, api_key, data=None):
        """Helper method to make authenticated API requests"""
        headers = {"HTTP_AUTHORIZATION": f"Token {api_key}", "HTTP_CONTENT_TYPE": "application/json"}

        if method.upper() == "GET":
            return self.client.get(url, **headers)
        elif method.upper() == "POST":
            return self.client.post(url, data=data, content_type="application/json", **headers)
        elif method.upper() == "PATCH":
            return self.client.patch(url, data=data, content_type="application/json", **headers)
        elif method.upper() == "DELETE":
            return self.client.delete(url, **headers)

    # Tests for Bot Create View (POST /api/bots)
    def test_bot_create_uses_correct_project(self):
        """Test that bot creation uses the project from the API key"""
        bot_data = {"bot_name": "Test Bot", "meeting_url": "https://meet.google.com/abc-def-ghi"}

        response = self._make_authenticated_request("POST", "/api/v1/bots", self.api_key_a_plain, json.dumps(bot_data))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created_bot = Bot.objects.get(object_id=response.json()["id"])
        self.assertEqual(created_bot.project, self.project_a)

    # Tests for Bot Detail View (GET/PATCH/DELETE /api/bots/<object_id>)
    def test_bot_detail_access_control(self):
        """Test that API key can only access bots in its own project"""
        # API key A can access bot A
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_a.object_id}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["id"], self.bot_a.object_id)

        # API key A cannot access bot B
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_b.object_id}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # API key B can access bot B
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_b.object_id}", self.api_key_b_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["id"], self.bot_b.object_id)

        # API key B cannot access bot A
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_a.object_id}", self.api_key_b_plain)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_bot_patch_access_control(self):
        """Test that PATCH requests respect project boundaries"""
        # Create a scheduled bot for patching
        scheduled_bot_a = Bot.objects.create(project=self.project_a, name="Scheduled Bot A", meeting_url="https://meet.google.com/scheduled-a", state=BotStates.SCHEDULED, join_at=datetime.now(timezone.utc))
        scheduled_bot_b = Bot.objects.create(project=self.project_b, name="Scheduled Bot B", meeting_url="https://meet.google.com/scheduled-b", state=BotStates.SCHEDULED, join_at=datetime.now(timezone.utc))

        patch_data = {"join_at": "2026-12-31T23:59:59Z"}

        # API key A can patch bot A
        response = self._make_authenticated_request("PATCH", f"/api/v1/bots/{scheduled_bot_a.object_id}", self.api_key_a_plain, json.dumps(patch_data))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # API key A cannot patch bot B
        response = self._make_authenticated_request("PATCH", f"/api/v1/bots/{scheduled_bot_b.object_id}", self.api_key_a_plain, json.dumps(patch_data))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_bot_delete_access_control(self):
        """Test that DELETE requests respect project boundaries"""
        # Create scheduled bots for deletion
        scheduled_bot_a = Bot.objects.create(project=self.project_a, name="Scheduled Bot A Delete", meeting_url="https://meet.google.com/delete-a", state=BotStates.SCHEDULED, join_at=datetime.now(timezone.utc))
        scheduled_bot_b = Bot.objects.create(project=self.project_b, name="Scheduled Bot B Delete", meeting_url="https://meet.google.com/delete-b", state=BotStates.SCHEDULED, join_at=datetime.now(timezone.utc))

        # API key A can delete bot A
        response = self._make_authenticated_request("DELETE", f"/api/v1/bots/{scheduled_bot_a.object_id}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # API key A cannot delete bot B
        response = self._make_authenticated_request("DELETE", f"/api/v1/bots/{scheduled_bot_b.object_id}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Tests for Bot Leave View (POST /api/bots/<object_id>/leave)
    def test_bot_leave_access_control(self):
        """Test that leave requests respect project boundaries"""
        # API key A can request bot A to leave
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.bot_a.object_id}/leave", self.api_key_a_plain, "{}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # API key A cannot request bot B to leave
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.bot_b.object_id}/leave", self.api_key_a_plain, "{}")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Tests for Transcript View (GET /api/bots/<object_id>/transcript)
    def test_transcript_access_control(self):
        """Test that transcript access respects project boundaries"""
        # API key A can access bot A's transcript
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_a.object_id}/transcript", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        transcript_data = response.json()
        self.assertEqual(len(transcript_data), 1)
        self.assertEqual(transcript_data[0]["transcription"]["transcript"], "Hello from bot A")

        # API key A cannot access bot B's transcript
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_b.object_id}/transcript", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_transcript_async_transcription_access_control(self):
        """Test that async transcription access respects project boundaries when using async_transcription_id parameter"""
        # API key A can access bot A's transcript with bot A's async transcription
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_a.object_id}/transcript?async_transcription_id={self.async_transcription_a.object_id}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # API key A cannot access bot A's transcript with bot B's async transcription (should fail because async transcription belongs to different recording)
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_a.object_id}/transcript?async_transcription_id={self.async_transcription_b.object_id}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("Async Transcription not found", response.json().get("error", ""))

        # API key A cannot access bot B's transcript with bot B's async transcription (should fail at bot level)
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_b.object_id}/transcript?async_transcription_id={self.async_transcription_b.object_id}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.json()["error"], "Bot not found")

        # API key B can access bot B's transcript with bot B's async transcription
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_b.object_id}/transcript?async_transcription_id={self.async_transcription_b.object_id}", self.api_key_b_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # API key B cannot access bot B's transcript with bot A's async transcription (should fail because async transcription belongs to different recording)
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_b.object_id}/transcript?async_transcription_id={self.async_transcription_a.object_id}", self.api_key_b_plain)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("Async Transcription not found", response.json().get("error", ""))

    def test_transcript_create_async_transcription_access_control(self):
        """Test that async transcription creation (POST /api/bots/<object_id>/transcript) respects project boundaries"""
        # API key A can create async transcription for bot A
        transcription_settings_json = json.dumps({"transcription_settings": {"deepgram": {"language": "en-US", "model": "nova-3"}}})
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.ended_bot_a.object_id}/transcript", self.api_key_a_plain, transcription_settings_json)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertIn("id", response_data)
        self.assertTrue(response_data["id"].startswith("tran_"))
        self.assertEqual(response_data["state"], "not_started")

        # API key A cannot create async transcription for bot B (should fail at bot level)
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.ended_bot_b.object_id}/transcript", self.api_key_a_plain, transcription_settings_json)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.json()["error"], "Bot not found")

        # API key B can create async transcription for bot B
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.ended_bot_b.object_id}/transcript", self.api_key_b_plain, transcription_settings_json)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response_data = response.json()
        self.assertIn("id", response_data)
        self.assertTrue(response_data["id"].startswith("tran_"))
        self.assertEqual(response_data["state"], "not_started")

        # API key B cannot create async transcription for bot A (should fail at bot level)
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.ended_bot_a.object_id}/transcript", self.api_key_b_plain, transcription_settings_json)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.json()["error"], "Bot not found")

    # Tests for Recording View (GET /api/bots/<object_id>/recording)
    def test_recording_access_control(self):
        """Test that recording access respects project boundaries"""
        # API key A can access bot A's recording
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_a.object_id}/recording", self.api_key_a_plain)
        # Recording might not have a file, so we expect either 200 or 404 for "No recording file found"
        self.assertIn(response.status_code, [status.HTTP_200_OK, status.HTTP_404_NOT_FOUND])
        if response.status_code == status.HTTP_404_NOT_FOUND:
            self.assertIn("No recording", response.json().get("error", ""))

        # API key A cannot access bot B's recording
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_b.object_id}/recording", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.json()["error"], "Bot not found")

    # Tests for Chat Messages View (GET /api/bots/<object_id>/chat_messages)
    def test_chat_messages_access_control(self):
        """Test that chat messages access respects project boundaries"""
        # API key A can access bot A's chat messages
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_a.object_id}/chat_messages", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Response should be paginated
        results = response.json().get("results", response.json())
        if isinstance(results, list):
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["text"], "Chat message from bot A")

        # API key A cannot access bot B's chat messages
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_b.object_id}/chat_messages", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Tests for Send Chat Message View (POST /api/bots/<object_id>/send_chat_message)
    def test_send_chat_message_access_control(self):
        """Test that sending chat messages respects project boundaries"""
        chat_data = {"to": "everyone", "message": "Test message"}

        # API key A can send message to bot A
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.bot_a.object_id}/send_chat_message", self.api_key_a_plain, json.dumps(chat_data))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # API key A cannot send message to bot B
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.bot_b.object_id}/send_chat_message", self.api_key_a_plain, json.dumps(chat_data))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Tests for Speech View (POST /api/bots/<object_id>/speech)
    def test_speech_access_control(self):
        """Test that speech requests respect project boundaries"""
        speech_data = {"text": "Hello, this is a test speech", "text_to_speech_settings": {"google": {"voice_language_code": "en-US", "voice_name": "en-US-Casual-K"}}}

        # API key A cannot send speech to bot A without Google TTS credentials
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.bot_a.object_id}/speech", self.api_key_a_plain, json.dumps(speech_data))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Google Text-to-Speech credentials are required", response.json()["error"])

        # API key A cannot send speech to bot B
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.bot_b.object_id}/speech", self.api_key_a_plain, json.dumps(speech_data))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Tests for Output Audio View (POST /api/bots/<object_id>/output_audio)
    def test_output_audio_access_control(self):
        """Test that audio output requests respect project boundaries"""
        # Create valid base64 encoded audio data
        audio_data = self.minimal_mp3_file_base64
        output_data = {"type": "audio/mp3", "data": audio_data}

        # API key A can send audio to bot A
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.bot_a.object_id}/output_audio", self.api_key_a_plain, json.dumps(output_data))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # API key A cannot send audio to bot B
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.bot_b.object_id}/output_audio", self.api_key_a_plain, json.dumps(output_data))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Tests for Output Image View (POST /api/bots/<object_id>/output_image)
    def test_output_image_access_control(self):
        """Test that image output requests respect project boundaries"""

        image_data = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
        output_data = {"type": "image/png", "data": image_data}

        # API key A can send image to bot A
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.bot_a.object_id}/output_image", self.api_key_a_plain, json.dumps(output_data))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # API key A cannot send image to bot B
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.bot_b.object_id}/output_image", self.api_key_a_plain, json.dumps(output_data))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Tests for Output Video View (POST /api/bots/<object_id>/output_video)
    def test_output_video_access_control(self):
        """Test that video output requests respect project boundaries"""
        output_data = {"url": "https://example.com/video.mp4"}

        # API key A can send video to bot A (if meeting type supports it)
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.bot_a.object_id}/output_video", self.api_key_a_plain, json.dumps(output_data))
        # May fail due to meeting type or feature not supported, but should not be 404 for "Bot not found"
        self.assertNotEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # API key A cannot send video to bot B
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.bot_b.object_id}/output_video", self.api_key_a_plain, json.dumps(output_data))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Tests for Delete Data View (POST /api/bots/<object_id>/delete_data)
    def test_delete_data_access_control(self):
        """Test that data deletion requests respect project boundaries"""
        # Create ended bots for data deletion (data can only be deleted for ended/failed bots)
        ended_bot_a = Bot.objects.create(project=self.project_a, name="Ended Bot A", meeting_url="https://meet.google.com/ended-a", state=BotStates.ENDED)
        ended_bot_b = Bot.objects.create(project=self.project_b, name="Ended Bot B", meeting_url="https://meet.google.com/ended-b", state=BotStates.ENDED)

        # API key A can delete data for bot A
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{ended_bot_a.object_id}/delete_data", self.api_key_a_plain, "{}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # API key A cannot delete data for bot B
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{ended_bot_b.object_id}/delete_data", self.api_key_a_plain, "{}")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Tests for Pause Recording View (POST /api/bots/<object_id>/pause_recording)
    def test_pause_recording_access_control(self):
        """Test that pause recording requests respect project boundaries"""
        # API key A can pause recording for bot A
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.bot_a.object_id}/pause_recording", self.api_key_a_plain, "{}")
        # May fail due to meeting type not supporting pause, but should not be 404 for "Bot not found"
        self.assertNotEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # API key A cannot pause recording for bot B
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{self.bot_b.object_id}/pause_recording", self.api_key_a_plain, "{}")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Tests for Resume Recording View (POST /api/bots/<object_id>/resume_recording)
    def test_resume_recording_access_control(self):
        """Test that resume recording requests respect project boundaries"""
        # Create bots in paused recording state
        paused_bot_a = Bot.objects.create(project=self.project_a, name="Paused Bot A", meeting_url="https://meet.google.com/paused-a", state=BotStates.JOINED_RECORDING_PAUSED)
        paused_bot_b = Bot.objects.create(project=self.project_b, name="Paused Bot B", meeting_url="https://meet.google.com/paused-b", state=BotStates.JOINED_RECORDING_PAUSED)

        # API key A can resume recording for bot A
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{paused_bot_a.object_id}/resume_recording", self.api_key_a_plain, "{}")
        # Should not be "Bot not found"
        self.assertNotEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # API key A cannot resume recording for bot B
        response = self._make_authenticated_request("POST", f"/api/v1/bots/{paused_bot_b.object_id}/resume_recording", self.api_key_a_plain, "{}")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Tests for Participant Events View (GET /api/bots/<object_id>/participant_events)
    def test_participant_events_access_control(self):
        """Test that participant events access respects project boundaries"""
        # API key A can access bot A's participant events
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_a.object_id}/participant_events", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Response should be paginated
        results = response.json().get("results", response.json())
        if isinstance(results, list):
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["event_type"], "join")

        # API key A cannot access bot B's participant events
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_b.object_id}/participant_events", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Tests for Participants View (GET /api/bots/<object_id>/participants)
    def test_participants_access_control(self):
        """Test that participants access respects project boundaries"""
        # API key A can access bot A's participants
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_a.object_id}/participants", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Response should be paginated
        results = response.json().get("results", response.json())
        if isinstance(results, list):
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["is_host"], False)

        # API key A cannot access bot B's participants
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_b.object_id}/participants", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Test for cross-project object access protection
    def test_cross_project_object_protection(self):
        """Test that objects from one project cannot be accessed via API key from another project"""
        # Try to access all bot B objects using API key A
        endpoints_to_test = [
            ("GET", f"/api/v1/bots/{self.bot_b.object_id}"),
            ("GET", f"/api/v1/bots/{self.bot_b.object_id}/transcript"),
            ("GET", f"/api/v1/bots/{self.bot_b.object_id}/recording"),
            ("GET", f"/api/v1/bots/{self.bot_b.object_id}/chat_messages"),
            ("GET", f"/api/v1/bots/{self.bot_b.object_id}/participant_events"),
            ("GET", f"/api/v1/bots/{self.bot_b.object_id}/participants"),
            ("POST", f"/api/v1/bots/{self.bot_b.object_id}/leave"),
            ("POST", f"/api/v1/bots/{self.bot_b.object_id}/send_chat_message"),
            ("POST", f"/api/v1/bots/{self.bot_b.object_id}/delete_data"),
        ]

        for method, endpoint in endpoints_to_test:
            with self.subTest(method=method, endpoint=endpoint):
                response = self._make_authenticated_request(method, endpoint, self.api_key_a_plain, "{}")
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(response.json()["error"], "Bot not found")

    def test_invalid_api_key_returns_401(self):
        """Test that invalid API keys return 401 Unauthorized"""
        response = self._make_authenticated_request("GET", f"/api/v1/bots/{self.bot_a.object_id}", "invalid_api_key")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_missing_authorization_header_returns_401(self):
        """Test that missing authorization header returns 401 Unauthorized"""
        response = self.client.get(f"/api/v1/bots/{self.bot_a.object_id}")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_nonexistent_bot_returns_404(self):
        """Test that requests for non-existent bots return 404"""
        response = self._make_authenticated_request("GET", "/api/v1/bots/bot_nonexistent12345", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.json()["error"], "Bot not found")

    # Tests for Bot List View (GET /api/bots)
    def test_bot_list_access_control(self):
        """Test that bot list only returns bots from the authenticated project"""
        # API key A can only see bots from project A
        response = self._make_authenticated_request("GET", "/api/v1/bots", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", response.json())
        if isinstance(results, list):
            # Should only see bot_a, not bot_b
            bot_ids = [bot["id"] for bot in results]
            self.assertIn(self.bot_a.object_id, bot_ids)
            self.assertNotIn(self.bot_b.object_id, bot_ids)

        # API key B can only see bots from project B
        response = self._make_authenticated_request("GET", "/api/v1/bots", self.api_key_b_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", response.json())
        if isinstance(results, list):
            # Should only see bot_b, not bot_a
            bot_ids = [bot["id"] for bot in results]
            self.assertIn(self.bot_b.object_id, bot_ids)
            self.assertNotIn(self.bot_a.object_id, bot_ids)

    def test_bot_list_meeting_url_filter(self):
        """Test filtering bots by meeting_url"""
        # Create additional bots with different meeting URLs
        Bot.objects.create(project=self.project_a, name="Bot A2", meeting_url="https://meet.google.com/different-url", state=BotStates.JOINED_RECORDING)

        # Test filtering by exact meeting URL
        response = self._make_authenticated_request("GET", f"/api/v1/bots?meeting_url={self.bot_a.meeting_url}", self.api_key_a_plain)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        results = response.json().get("results", response.json())
        if isinstance(results, list):
            # Should only return the bot with matching meeting URL
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["id"], self.bot_a.object_id)
            self.assertEqual(results[0]["meeting_url"], self.bot_a.meeting_url)
