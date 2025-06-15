import json
import logging
import os
from enum import Enum

import redis
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse

from .models import (
    Bot,
    BotChatMessageRequest,
    BotEventManager,
    BotEventTypes,
    BotMediaRequest,
    BotMediaRequestMediaTypes,
    BotStates,
    Credentials,
    MediaBlob,
    MeetingTypes,
    Project,
    Recording,
    TranscriptionTypes,
)
from .serializers import (
    CreateBotSerializer,
)
from .utils import meeting_type_from_url, transcription_provider_from_meeting_url_and_transcription_settings

logger = logging.getLogger(__name__)


def send_sync_command(bot, command="sync"):
    redis_url = os.getenv("REDIS_URL") + ("?ssl_cert_reqs=none" if os.getenv("DISABLE_REDIS_SSL") else "")
    redis_client = redis.from_url(redis_url)
    channel = f"bot_{bot.id}"
    message = {"command": command}
    redis_client.publish(channel, json.dumps(message))


def create_bot_chat_message_request(bot, chat_message_data):
    """
    Creates a BotChatMessageRequest for the given bot with the provided data.

    Args:
        bot: The Bot instance
        chat_message_data: Validated data containing to_user_uuid, to, and message

    Returns:
        BotChatMessageRequest: The created chat message request
    """
    try:
        bot_chat_message_request = BotChatMessageRequest.objects.create(
            bot=bot,
            to_user_uuid=chat_message_data.get("to_user_uuid"),
            to=chat_message_data["to"],
            message=chat_message_data["message"],
        )
    except Exception as e:
        error_message_first_line = str(e).split("\n")[0]
        logging.error(f"Error creating bot chat message request: {error_message_first_line}")
        raise ValidationError(f"Error creating the bot chat message request: {error_message_first_line}.")

    return bot_chat_message_request


def create_bot_media_request_for_image(bot, image):
    content_type = image["type"]
    image_data = image["decoded_data"]
    try:
        # Create or get existing MediaBlob
        media_blob = MediaBlob.get_or_create_from_blob(project=bot.project, blob=image_data, content_type=content_type)
    except Exception as e:
        error_message_first_line = str(e).split("\n")[0]
        logging.error(f"Error creating image blob: {error_message_first_line} (content_type={content_type})")
        raise ValidationError(f"Error creating the image blob: {error_message_first_line}.")

    # Create BotMediaRequest
    BotMediaRequest.objects.create(
        bot=bot,
        media_blob=media_blob,
        media_type=BotMediaRequestMediaTypes.IMAGE,
    )


def validate_meeting_url_and_credentials(meeting_url, project):
    """
    Validates meeting URL format and required credentials.
    Returns error message if validation fails, None if validation succeeds.
    """

    if meeting_type_from_url(meeting_url) == MeetingTypes.ZOOM:
        zoom_credentials = project.credentials.filter(credential_type=Credentials.CredentialTypes.ZOOM_OAUTH).first()
        if not zoom_credentials:
            relative_url = reverse("bots:project-credentials", kwargs={"object_id": project.object_id})
            settings_url = f"https://{os.getenv('SITE_DOMAIN', 'app.attendee.dev')}{relative_url}"
            return {"error": f"Zoom App credentials are required to create a Zoom bot. Please add Zoom credentials at {settings_url}"}

    return None


class BotCreationSource(str, Enum):
    API = "api"
    DASHBOARD = "dashboard"
    SCHEDULER = "scheduler"


def create_bot(data: dict, source: BotCreationSource, project: Project) -> tuple[Bot | None, dict | None]:
    # Given them a small grace period before we start rejecting requests
    if project.organization.credits() < -1:
        logger.error(f"Organization {project.organization.id} has insufficient credits. Please add credits in the Settings -> Billing page.")
        return None, {"error": "Organization has run out of credits. Please add more credits in the Settings -> Billing page."}

    serializer = CreateBotSerializer(data=data)
    if not serializer.is_valid():
        return None, serializer.errors

    # Access the bot through the api key
    meeting_url = serializer.validated_data["meeting_url"]

    error = validate_meeting_url_and_credentials(meeting_url, project)
    if error:
        return None, error

    bot_name = serializer.validated_data["bot_name"]
    transcription_settings = serializer.validated_data["transcription_settings"]
    rtmp_settings = serializer.validated_data["rtmp_settings"]
    recording_settings = serializer.validated_data["recording_settings"]
    debug_settings = serializer.validated_data["debug_settings"]
    automatic_leave_settings = serializer.validated_data["automatic_leave_settings"]
    bot_image = serializer.validated_data["bot_image"]
    bot_chat_message = serializer.validated_data["bot_chat_message"]
    metadata = serializer.validated_data["metadata"]
    join_at = serializer.validated_data["join_at"]
    initial_state = BotStates.SCHEDULED if join_at else BotStates.READY

    settings = {
        "transcription_settings": transcription_settings,
        "rtmp_settings": rtmp_settings,
        "recording_settings": recording_settings,
        "debug_settings": debug_settings,
        "automatic_leave_settings": automatic_leave_settings,
    }

    with transaction.atomic():
        bot = Bot.objects.create(
            project=project,
            meeting_url=meeting_url,
            name=bot_name,
            settings=settings,
            metadata=metadata,
            join_at=join_at,
            state=initial_state,
        )

        Recording.objects.create(
            bot=bot,
            recording_type=bot.recording_type(),
            transcription_type=TranscriptionTypes.NON_REALTIME,
            transcription_provider=transcription_provider_from_meeting_url_and_transcription_settings(meeting_url, transcription_settings),
            is_default_recording=True,
        )

        if bot_image:
            try:
                create_bot_media_request_for_image(bot, bot_image)
            except ValidationError as e:
                return None, {"error": e.messages[0]}

        if bot_chat_message:
            try:
                create_bot_chat_message_request(bot, bot_chat_message)
            except ValidationError as e:
                return None, {"error": e.messages[0]}

        if bot.state == BotStates.READY:
            # Try to transition the state from READY to JOINING
            BotEventManager.create_event(bot=bot, event_type=BotEventTypes.JOIN_REQUESTED, event_metadata={"source": source})

        return bot, None
