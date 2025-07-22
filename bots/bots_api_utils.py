import json
import logging
import os
from enum import Enum

import redis
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
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
    WebhookSecret,
    WebhookSubscription,
    WebhookTriggerTypes,
)
from .serializers import (
    CreateBotSerializer,
)
from .utils import meeting_type_from_url, transcription_provider_from_bot_creation_data

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
    if project.organization.out_of_credits():
        logger.error(f"Organization {project.organization.id} has insufficient credits. Please add credits in the Account -> Billing page.")
        return None, {"error": "Organization has run out of credits. Please add more credits in the Account -> Billing page."}

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
    initial_state = BotStates.SCHEDULED if join_at else BotStates.READY

    settings = {
        "transcription_settings": transcription_settings,
        "rtmp_settings": rtmp_settings,
        "recording_settings": recording_settings,
        "debug_settings": debug_settings,
        "automatic_leave_settings": automatic_leave_settings,
        "teams_settings": teams_settings,
        "zoom_settings": zoom_settings,
        "websocket_settings": websocket_settings,
        "callback_settings": callback_settings,
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

        logger.error(f"Error creating bot: {e}")
        return None, {"error": str(e)}


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
    if not url.startswith("https://"):
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
