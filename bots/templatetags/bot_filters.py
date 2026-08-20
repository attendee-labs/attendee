import hashlib
import json
from datetime import datetime

from django import template
from django.utils import timezone
from django.utils.timesince import timesince

from bots.models import WebhookTriggerTypes

register = template.Library()


@register.filter
def timesince_or_seconds(value, now=None):
    """Like Django's timesince, but renders sub-minute gaps in seconds instead of "0 minutes"."""
    if not value:
        return ""
    if now is None:
        now = timezone.now() if timezone.is_aware(value) else datetime.now()
    seconds = int((now - value).total_seconds())
    if 0 <= seconds < 60:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    return timesince(value, now)


@register.filter
def modulo(num, val):
    return int(num) % val


@register.filter
def integer_divide(num, val):
    return int(num) // val


@register.filter
def get_next(value, current_index):
    try:
        return value[current_index + 1]
    except IndexError:
        return value[current_index]  # fallback to current item if next doesn't exist


@register.filter
def participant_color(uuid):
    """Generate a consistent color from a participant's UUID"""
    if not uuid:
        return "#808080"  # Default gray for participants without UUID

    # Generate a hash of the UUID
    hash_object = hashlib.md5(str(uuid).encode())
    hash_hex = hash_object.hexdigest()

    # Use the first 6 characters of the hash as a color code
    # Adjust brightness to ensure readable colors (avoiding too light or dark)
    r = int(hash_hex[:2], 16)
    g = int(hash_hex[2:4], 16)
    b = int(hash_hex[4:6], 16)

    # Ensure minimum brightness
    min_brightness = 64
    r = max(r, min_brightness)
    g = max(g, min_brightness)
    b = max(b, min_brightness)

    # Ensure maximum brightness
    max_brightness = 200
    r = min(r, max_brightness)
    g = min(g, max_brightness)
    b = min(b, max_brightness)

    return f"#{r:02x}{g:02x}{b:02x}"


@register.filter
def md5(value):
    return hashlib.md5(str(value).encode()).hexdigest()


@register.filter
def epoch_to_datetime(value):
    """Convert epoch timestamp (seconds) to datetime object for use with date filter"""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value))
    except (ValueError, TypeError, OSError):
        return None


@register.filter
def pretty_json(value):
    """Format a value as indented JSON for display."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return value
    try:
        return json.dumps(value, indent=2, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return value


@register.filter
def map_trigger_types(trigger_or_triggers):
    """Transform webhook trigger types to their API codes, works for both single triggers and lists.
    Handles both integer enum values (legacy) and string API codes (current)."""
    if hasattr(trigger_or_triggers, "__iter__") and not isinstance(trigger_or_triggers, str):
        # It's a list/iterable
        result = []
        for trigger in trigger_or_triggers:
            if isinstance(trigger, str):
                # Already a string API code
                result.append(trigger)
            else:
                # Convert integer enum value to API code
                api_code = WebhookTriggerTypes.trigger_type_to_api_code(trigger)
                if api_code is not None:
                    result.append(api_code)
                # Skip None values to avoid displaying them in UI
        return result
    else:
        # Single trigger
        if isinstance(trigger_or_triggers, str):
            # Already a string API code
            return trigger_or_triggers
        else:
            # Convert integer enum value to API code
            api_code = WebhookTriggerTypes.trigger_type_to_api_code(trigger_or_triggers)
            return api_code if api_code is not None else f"unknown_trigger_{trigger_or_triggers}"
