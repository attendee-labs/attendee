import logging
import uuid

from django.db import IntegrityError

from .models import BotLoginGroup
from .serializers import CreateBotLoginGroupSerializer

logger = logging.getLogger(__name__)


def create_bot_login_group(data, project):
    """
    Create a new bot login group for the given project.

    Args:
        data: Dictionary containing bot login group creation data
        project: Project instance to associate the bot login group with

    Returns:
        tuple: (bot_login_group_instance, error_dict)
               Returns (BotLoginGroup, None) on success
               Returns (None, error_dict) on failure
    """
    # Validate the input data
    serializer = CreateBotLoginGroupSerializer(data=data)
    if not serializer.is_valid():
        return None, serializer.errors

    validated_data = serializer.validated_data
    platform = validated_data["platform"]
    name = validated_data["name"]

    if BotLoginGroup.objects.filter(project=project, platform=platform, name=name).exists():
        return None, {"error": "A login group for this platform with this name already exists"}

    try:
        bot_login_group = BotLoginGroup.objects.create(project=project, platform=platform, name=name)
        return bot_login_group, None
    except IntegrityError as e:
        error_id = str(uuid.uuid4())
        logger.error(f"Error creating bot login group (error_id={error_id}): {e}")
        return None, {"non_field_errors": ["An error occurred while creating the bot login group. Error ID: " + error_id]}
    except Exception as e:
        error_id = str(uuid.uuid4())
        logger.error(f"Error creating bot login group (error_id={error_id}): {e}")
        return None, {"non_field_errors": ["An unexpected error occurred while creating the bot login group. Error ID: " + error_id]}
