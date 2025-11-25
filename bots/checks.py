"""
Django system checks for the bots app.
"""

import logging

from django.conf import settings
from django.core.checks import Error, register

from bots.bot_pod_creator.bot_pod_creator import BotPodCreator
from bots.bot_pod_creator.bot_pod_spec import BotPodSpecType

logger = logging.getLogger(__name__)


@register("kubernetes")
def check_bot_pod_specs(app_configs, **kwargs):
    """
    Validates that all bot pod spec types can be successfully created in dry-run mode.

    This check loops over all possible bot pod spec types (from BotPodSpecType enum
    and CUSTOM_BOT_POD_SPEC_TYPES setting) and attempts to create a pod with each
    spec in dry-run mode. If any spec fails, an error is returned.
    """
    errors = []

    # Collect all spec types to check
    spec_types = list(BotPodSpecType.__members__.keys())

    # Add custom spec types from settings
    if hasattr(settings, "CUSTOM_BOT_POD_SPEC_TYPES"):
        custom_types = settings.CUSTOM_BOT_POD_SPEC_TYPES
        if custom_types:
            spec_types.extend(custom_types)

    # Try to create BotPodCreator instance
    try:
        creator = BotPodCreator()
    except Exception as e:
        errors.append(
            Error(
                f"Failed to initialize BotPodCreator: {e}",
                hint="Check that Kubernetes configuration is available and CUBER_RELEASE_VERSION is set.",
                id="bots.E001",
            )
        )
        # Cannot proceed with checks if creator fails to initialize
        return errors

    # Test each spec type with a dry run
    for spec_type in spec_types:
        try:
            # Attempt dry run with a test bot ID
            result = creator.create_bot_pod(
                bot_id=999999,  # Using a test bot ID that won't conflict
                bot_name=f"test-bot-{spec_type.lower()}-validation",
                bot_pod_spec_type=spec_type,
                dry_run=True,
            )

            # Check if the dry run reported any errors
            if not result.get("created", False):
                error_msg = result.get("error", "Unknown error")
                errors.append(
                    Error(
                        f"Bot pod spec type '{spec_type}' dry run failed: {error_msg}",
                        hint=f"Check BOT_POD_SPEC_{spec_type} environment variable and its JSON patch syntax.",
                        id=f"bots.E002.{spec_type}",
                    )
                )
        except ValueError as e:
            # Validation errors (e.g., invalid spec type format)
            errors.append(
                Error(
                    f"Bot pod spec type '{spec_type}' validation failed: {e}",
                    hint=f"Ensure BOT_POD_SPEC_{spec_type} is properly configured.",
                    id=f"bots.E003.{spec_type}",
                )
            )
        except Exception as e:
            # Unexpected errors
            errors.append(
                Error(
                    f"Unexpected error testing bot pod spec type '{spec_type}': {e}",
                    hint="Check logs for more details.",
                    id=f"bots.E004.{spec_type}",
                )
            )

    return errors
