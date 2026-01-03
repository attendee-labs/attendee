import json
import logging
import os
import uuid
from enum import Enum

import redis
from concurrency.exceptions import RecordModifiedError
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from .meeting_url_utils import meeting_type_from_url
from .models import (
    Bot,
    BotChatMessageRequest,
    BotEventManager,
    BotEventTypes,
    BotMediaRequest,
    BotMediaRequestMediaTypes,
    BotStates,
    CalendarEvent,
    Credentials,
    MediaBlob,
    MeetingTypes,
    Project,
    Recording,
    TranscriptionProviders,
    TranscriptionSettings,
    TranscriptionTypes,
    WebhookSecret,
    WebhookSubscription,
    WebhookTriggerTypes,
)
from .serializers import (
    CreateBotSerializer,
    PatchBotSerializer,
    PatchBotTranscriptionSettingsSerializer,
    PatchBotVoiceAgentSettingsSerializer,
)
from .utils import transcription_provider_from_bot_creation_data

logger = logging.getLogger(__name__)


def build_site_url(path=""):
    """
    Build a full URL using SITE_DOMAIN setting.
    Automatically uses http:// for localhost, https:// for everything else.
    """
    protocol = "http" if settings.SITE_DOMAIN.startswith("localhost") else "https"
    return f"{protocol}://{settings.SITE_DOMAIN}{path}"


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
        # Make sure the bot has a participant with the given to_user_uuid
        to_user_uuid = chat_message_data.get("to_user_uuid")
        if to_user_uuid:
            participant = bot.participants.filter(uuid=to_user_uuid).first()
            if not participant:
                raise ValidationError(f"No participant found with uuid {to_user_uuid}. Use the /participants endpoint to get the list of participants.", params={"to_user_uuid": to_user_uuid})

        bot_chat_message_request = BotChatMessageRequest.objects.create(
            bot=bot,
            to_user_uuid=to_user_uuid,
            to=chat_message_data["to"],
            message=chat_message_data["message"],
        )
    except ValidationError as e:
        raise e
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
        zoom_credentials = project.credentials.filter(credential_type=Credentials.CredentialTypes.ZOOM_OAUTH).first() or project.zoom_oauth_apps.first()
        if not zoom_credentials:
            relative_url = reverse("bots:project-credentials", kwargs={"object_id": project.object_id})
            settings_url = build_site_url(relative_url)
            return {"error": f"Zoom App credentials are required to create a Zoom bot. Please add Zoom credentials at {settings_url}"}

    return None


def validate_bot_concurrency_limit(project):
    active_bots_count = Bot.objects.filter(project=project).filter(BotEventManager.get_in_meeting_states_q_filter()).count()
    concurrent_bots_limit = project.concurrent_bots_limit()
    if active_bots_count >= concurrent_bots_limit:
        logger.error(f"Project {project.object_id} has exceeded the maximum number of concurrent bots ({concurrent_bots_limit}).")
        return {"error": f"You have exceeded the maximum number of concurrent bots ({concurrent_bots_limit}) for your account. Please reach out to customer support to increase the limit."}

    return None


# Returns a tuple of (calendar_event, error)
# Side effect: sets the meeting_url and join_at in the data dictionary if the calendar event is found
def initialize_bot_creation_data_from_calendar_event(data, project):
    calendar_event = None
    if data.get("calendar_event_id"):
        try:
            calendar_event = CalendarEvent.objects.get(object_id=data["calendar_event_id"], calendar__project=project)
        except CalendarEvent.DoesNotExist:
            return None, {"error": f"Calendar event with id {data['calendar_event_id']} does not exist in this project."}

        if data.get("meeting_url"):
            return None, {"error": "meeting_url should not be provided when calendar_event_id is specified. The meeting URL will be taken from the calendar event."}
        data["meeting_url"] = calendar_event.meeting_url

        if data.get("join_at"):
            return None, {"error": "join_at should not be provided when calendar_event_id is specified. The join time will be taken from the calendar event."}
        data["join_at"] = calendar_event.start_time

    return calendar_event, None


def validate_external_media_storage_settings(external_media_storage_settings, project):
    if not external_media_storage_settings:
        return None

    if not project.credentials.filter(credential_type=Credentials.CredentialTypes.EXTERNAL_MEDIA_STORAGE).exists():
        relative_url = reverse("bots:project-credentials", kwargs={"object_id": project.object_id})
        settings_url = build_site_url(relative_url)
        return {"error": f"External media storage credentials are required to upload recordings to an external storage bucket. Please add external media storage credentials at {settings_url}."}

    return None


class BotCreationSource(str, Enum):
    API = "api"
    DASHBOARD = "dashboard"
    SCHEDULER = "scheduler"


def create_bot(data: dict, source: BotCreationSource, project: Project) -> tuple[Bot | None, dict | None]:
    # Given them a small grace period before we start rejecting requests
    if project.organization.out_of_credits():
        logger.error(f"Organization {project.organization.id} has insufficient credits. Please add credits in the Account -> Billing page.")
        return None, {"error": "Organization has run out of credits. Please add more credits in the Account -> Billing page."}

    # Do some initialization of the data if the calendar event id was provided
    calendar_event, error = initialize_bot_creation_data_from_calendar_event(data, project)
    if error:
        return None, error

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
    google_meet_settings = serializer.validated_data["google_meet_settings"]
    teams_settings = serializer.validated_data["teams_settings"]
    zoom_settings = serializer.validated_data["zoom_settings"]
    bot_image = serializer.validated_data["bot_image"]
    bot_chat_message = serializer.validated_data["bot_chat_message"]
    metadata = serializer.validated_data["metadata"]
    websocket_settings = serializer.validated_data["websocket_settings"]
    join_at = serializer.validated_data["join_at"]
    deduplication_key = serializer.validated_data["deduplication_key"]
    webhook_subscriptions = serializer.validated_data["webhooks"]
    callback_settings = serializer.validated_data["callback_settings"]
    external_media_storage_settings = serializer.validated_data["external_media_storage_settings"]
    voice_agent_settings = serializer.validated_data["voice_agent_settings"]
    initial_state = BotStates.SCHEDULED if join_at else BotStates.READY

    error = validate_external_media_storage_settings(external_media_storage_settings, project)
    if error:
        return None, error

    error = validate_bot_concurrency_limit(project)
    if error:
        return None, error

    settings = {
        "transcription_settings": transcription_settings,
        "rtmp_settings": rtmp_settings,
        "recording_settings": recording_settings,
        "debug_settings": debug_settings,
        "automatic_leave_settings": automatic_leave_settings,
        "google_meet_settings": google_meet_settings,
        "teams_settings": teams_settings,
        "zoom_settings": zoom_settings,
        "websocket_settings": websocket_settings,
        "callback_settings": callback_settings,
        "external_media_storage_settings": external_media_storage_settings,
        "voice_agent_settings": voice_agent_settings,
    }

    try:
        with transaction.atomic():
            bot = Bot.objects.create(
                project=project,
                meeting_url=meeting_url,
                name=bot_name,
                settings=settings,
                metadata=metadata,
                join_at=join_at,
                deduplication_key=deduplication_key,
                state=initial_state,
                calendar_event=calendar_event,
            )

            Recording.objects.create(
                bot=bot,
                recording_type=bot.recording_type(),
                transcription_type=TranscriptionTypes.NON_REALTIME,
                transcription_provider=transcription_provider_from_bot_creation_data(serializer.validated_data),
                is_default_recording=True,
            )

            if bot_image:
                create_bot_media_request_for_image(bot, bot_image)

            if bot_chat_message:
                create_bot_chat_message_request(bot, bot_chat_message)

            # Create bot-level webhook subscriptions if provided
            if webhook_subscriptions:
                create_webhook_subscriptions(webhook_subscriptions, project, bot)

            if bot.state == BotStates.READY:
                # Try to transition the state from READY to JOINING
                BotEventManager.create_event(bot=bot, event_type=BotEventTypes.JOIN_REQUESTED, event_metadata={"source": source})

            return bot, None

    except ValidationError as e:
        logger.error(f"ValidationError creating bot: {e}")
        return None, {"error": e.messages[0]}
    except Exception as e:
        if isinstance(e, IntegrityError) and "unique_bot_deduplication_key" in str(e):
            logger.error(f"IntegrityError due to unique_bot_deduplication_key constraint violation creating bot: {e}")
            return None, {"error": "Deduplication key already in use. A bot in a non-terminal state with this deduplication key already exists. Please use a different deduplication key or wait for that bot to terminate."}

        error_id = str(uuid.uuid4())
        logger.error(f"Error creating bot (error_id={error_id}): {e}")
        return None, {"error": f"An error occurred while creating the bot. Error ID: {error_id}"}


def patch_bot_voice_agent_settings(bot: Bot, data: dict) -> tuple[Bot | None, dict | None]:
    # Check if bot is in a state that allows updating voice agent settings
    if not BotEventManager.is_state_that_can_update_voice_agent_settings(bot.state):
        return None, {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot update voice agent settings"}

    # Check if bot launched a webpage streamer
    if not bot.should_launch_webpage_streamer():
        return None, {"error": "Voice agent resources were not reserved. You must create the bot with voice_agent_settings.reserve_resources set to true."}

    # Validate the request data
    serializer = PatchBotVoiceAgentSettingsSerializer(data=data)
    if not serializer.is_valid():
        return None, serializer.errors

    validated_data = serializer.validated_data

    # Update the bot in the DB. Handle concurrency conflict
    # Only legal update is to update the teams closed captions language
    try:
        if "url" in validated_data:
            bot.settings["voice_agent_settings"]["url"] = validated_data.get("url")
            if "screenshare_url" in bot.settings["voice_agent_settings"]:
                del bot.settings["voice_agent_settings"]["screenshare_url"]
        if "screenshare_url" in validated_data:
            bot.settings["voice_agent_settings"]["screenshare_url"] = validated_data.get("screenshare_url")
            if "url" in bot.settings["voice_agent_settings"]:
                del bot.settings["voice_agent_settings"]["url"]
        bot.save()
    except RecordModifiedError:
        return None, {"error": "Version conflict. Please try again."}

    return bot, None


def patch_bot_transcription_settings(bot: Bot, data: dict) -> tuple[Bot | None, dict | None]:
    # Check if bot is in a state that allows updating transcription settings
    if not BotEventManager.is_state_that_can_update_transcription_settings(bot.state):
        return None, {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} and cannot update transcription settings"}

    default_recording = Recording.objects.get(bot=bot, is_default_recording=True)
    if default_recording.transcription_provider != TranscriptionProviders.CLOSED_CAPTION_FROM_PLATFORM:
        return None, {"error": "Bot is not transcribing with meeting closed captions"}

    # Validate the request data
    serializer = PatchBotTranscriptionSettingsSerializer(data=data)
    if not serializer.is_valid():
        return None, serializer.errors

    validated_data = serializer.validated_data

    # Update the bot in the DB. Handle concurrency conflict
    # Only legal update is to update the teams closed captions language or google meet closed captions language
    try:
        if "transcription_settings" not in bot.settings:
            bot.settings["transcription_settings"] = {}
        if "meeting_closed_captions" not in bot.settings["transcription_settings"]:
            bot.settings["transcription_settings"]["meeting_closed_captions"] = {}
        new_transcription_settings = TranscriptionSettings(validated_data.get("transcription_settings"))
        new_teams_language = new_transcription_settings.teams_closed_captions_language()
        if new_teams_language:
            bot.settings["transcription_settings"]["meeting_closed_captions"]["teams_language"] = new_teams_language
        new_google_meet_language = new_transcription_settings.google_meet_closed_captions_language()
        if new_google_meet_language:
            bot.settings["transcription_settings"]["meeting_closed_captions"]["google_meet_language"] = new_google_meet_language
        bot.save()
    except RecordModifiedError:
        return None, {"error": "Version conflict. Please try again."}

    return bot, None


def patch_bot(bot: Bot, data: dict) -> tuple[Bot | None, dict | None]:
    """
    Updates a scheduled bot with the provided data.

    Args:
        bot: The Bot instance to update
        data: Dictionary containing the fields to update

    Returns:
        tuple: (updated_bot, error) where one is None
    """

    # Validate the request data
    serializer = PatchBotSerializer(data=data)
    if not serializer.is_valid():
        return None, serializer.errors

    validated_data = serializer.validated_data

    try:
        # Update the bot
        previous_join_at = bot.join_at
        bot.join_at = validated_data.get("join_at", bot.join_at)
        previous_meeting_url = bot.meeting_url
        bot.meeting_url = validated_data.get("meeting_url", bot.meeting_url)
        bot.metadata = validated_data.get("metadata", bot.metadata)

        # If the join_at or meeting_url is being updated, the state must be scheduled. If it isn't error out.
        update_only_legal_for_scheduled_bots = bot.join_at != previous_join_at or bot.meeting_url != previous_meeting_url
        if update_only_legal_for_scheduled_bots and bot.state != BotStates.SCHEDULED:
            return None, {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} but the join_at or meeting_url can only be updated when in the scheduled state"}

        bot.save()

        return bot, None

    except ValidationError as e:
        logger.error(f"ValidationError patching bot: {e}")
        return None, {"error": e.messages[0]}
    except Exception as e:
        error_id = str(uuid.uuid4())
        logger.error(f"Error patching bot (error_id={error_id}): {e}")
        return None, {"error": f"An error occurred while patching the bot. Error ID: {error_id}"}


def delete_bot(bot: Bot) -> tuple[bool, dict | None]:
    """
    Deletes a scheduled bot.

    Args:
        bot: The Bot instance to delete

    Returns:
        tuple: (success, error) where success is True if deletion succeeded,
               and error is None on success or error dict on failure
    """
    # Check if bot is in scheduled state
    if bot.state != BotStates.SCHEDULED:
        return False, {"error": f"Bot is in state {BotStates.state_to_api_code(bot.state)} but can only be deleted when in scheduled state"}

    try:
        bot.delete()
        return True, None

    except ValidationError as e:
        logger.error(f"ValidationError deleting bot: {e}")
        return False, {"error": e.messages[0]}
    except Exception as e:
        error_id = str(uuid.uuid4())
        logger.error(f"Error deleting bot (error_id={error_id}): {e}")
        return False, {"error": f"An error occurred while deleting the bot. Error ID: {error_id}"}


def validate_webhook_data(url, triggers, project, bot=None):
    """
    Validates webhook URL and triggers for both project-level and bot-level webhooks.
    Returns error message if validation fails.

    Args:
        url: The webhook URL
        triggers: List of trigger types as strings
        project: The Project instance
        bot: Optional Bot instance for bot-level webhooks

    Returns:
        error_message: None if validation succeeds, otherwise an error message
    """

    # Check if the trigger codes are valid
    for trigger in triggers:
        if WebhookTriggerTypes.api_code_to_trigger_type(trigger) is None:
            return f"Invalid webhook trigger type: {trigger}"

    # Check if URL is valid
    if not url.startswith("https://") and settings.REQUIRE_HTTPS_WEBHOOKS:
        return "webhook URL must start with https://"

    # Check for duplicate URLs
    existing_webhook_query = project.webhook_subscriptions.filter(url=url)
    if bot:
        # For bot-level webhooks, check if URL already exists for this bot
        if existing_webhook_query.filter(bot=bot).exists():
            return "URL already subscribed for this bot"
    else:
        # For project-level webhooks, check if URL already exists for project
        if existing_webhook_query.filter(bot__isnull=True).exists():
            return "URL already subscribed"

    # Webhook limit check
    if bot:
        # For bot-level webhooks, check the limit (only count bot-level webhooks)
        bot_level_webhooks = WebhookSubscription.objects.filter(project=project, bot=bot).count()
        if bot_level_webhooks >= 2:
            return "You have reached the maximum number of webhooks for a single bot"
    else:
        # For project-level webhooks, check the limit (only count project-level webhooks)
        project_level_webhooks = WebhookSubscription.objects.filter(project=project, bot__isnull=True).count()
        if project_level_webhooks >= 2:
            return "You have reached the maximum number of webhooks"

    # If we get here, the webhook data is valid
    return None


def create_webhook_subscription(url, triggers, project, bot=None):
    """
    Creates a single webhook subscription for a project or bot.

    Args:
        url: The webhook URL
        triggers: List of trigger types (api codes as strings)
        project: The Project instance
        bot: Optional Bot instance for bot-level webhooks

    Returns:
        None

    Raises:
        ValidationError: If the webhook data is invalid
    """
    # Validate the webhook data
    error = validate_webhook_data(url, triggers, project, bot)
    if error:
        raise ValidationError(error)

    # Get or create webhook secret for the project
    WebhookSecret.objects.get_or_create(project=project)

    # Map the triggers to integers
    triggers_mapped_to_integers = [WebhookTriggerTypes.api_code_to_trigger_type(trigger) for trigger in triggers]

    # Create the webhook subscription
    WebhookSubscription.objects.create(
        project=project,
        bot=bot,
        url=url,
        triggers=triggers_mapped_to_integers,
    )


def create_webhook_subscriptions(webhook_data_list, project, bot=None):
    """
    Creates multiple webhook subscriptions for a project or bot.

    Args:
        webhook_data_list: List of webhook data dictionaries with 'url' and 'triggers'
        project: The Project instance
        bot: Optional Bot instance for bot-level webhooks

    Returns:
        None

    Raises:
        ValidationError: If the webhook data is invalid
        Exception: If there is an error creating the webhook subscriptions
    """
    if not webhook_data_list:
        return

    # Create all webhook subscriptions
    for webhook_data in webhook_data_list:
        url = webhook_data.get("url", "")
        triggers = webhook_data.get("triggers", [])

        create_webhook_subscription(url, triggers, project, bot)
