from __future__ import annotations

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction

from notifications.models import NotificationCategory, TeamNotificationPreference, UserNotificationPreference

CACHE_TTL_SECONDS = 60

USER_ALLOWED_CATEGORY_KEYS = {
    "event_reminder",
    "teams",
    "meeting_summary",
    "comments",
    "clips",
    "product_updates",
    "workspace_members",
}

TEAM_ALLOWED_CATEGORY_KEYS = {
    "meetings",
    "action_items",
    "teams",
    "members",
}


def _user_cache_key(user_id: str) -> str:
    return f"notifications:user:{user_id}"


def _team_cache_key(team_id: int) -> str:
    return f"notifications:team:{team_id}"


def get_all_category_ids(*, allowed_keys: set[str] | None = None):
    qs = NotificationCategory.objects.all()
    if allowed_keys:
        qs = qs.filter(key__in=allowed_keys)
    return list(qs.values_list("id", flat=True))


def resolve_category_by_key_or_id(*, category_id=None, category_key=None):
    if category_id:
        try:
            return NotificationCategory.objects.get(pk=category_id)
        except NotificationCategory.DoesNotExist as exc:
            raise ValidationError({"category_id": "Invalid category_id"}) from exc
    if category_key:
        try:
            return NotificationCategory.objects.get(key=category_key)
        except NotificationCategory.DoesNotExist as exc:
            raise ValidationError({"category_key": "Invalid category_key"}) from exc
    return None


def _compute_user_eligible(user_id: str):
    prefs = UserNotificationPreference.objects.filter(user_id=user_id)
    if not prefs.exists():
        return get_all_category_ids(allowed_keys=USER_ALLOWED_CATEGORY_KEYS)
    if prefs.filter(category__isnull=True).exists():
        return []
    return list(prefs.filter(category__isnull=False).values_list("category_id", flat=True))


def get_eligible_categories_for_user(user_id: str):
    key = _user_cache_key(user_id)
    cached = cache.get(key)
    if cached is not None:
        return cached
    eligible = _compute_user_eligible(user_id)
    cache.set(key, eligible, CACHE_TTL_SECONDS)
    return eligible


def replace_user_preferences(user_id: str, category_ids: list | None) -> dict:
    with transaction.atomic():
        UserNotificationPreference.objects.filter(user_id=user_id).delete()

        if category_ids is None:
            cache.delete(_user_cache_key(user_id))
            return {"mode": "default", "category_ids": []}

        if len(category_ids) == 0:
            UserNotificationPreference.objects.create(user_id=user_id, category=None)
            cache.set(_user_cache_key(user_id), [], CACHE_TTL_SECONDS)
            return {"mode": "block_all", "category_ids": []}

        categories = list(
            NotificationCategory.objects.filter(id__in=category_ids, key__in=USER_ALLOWED_CATEGORY_KEYS)
        )
        if len(categories) != len(set(category_ids)):
            raise ValidationError({"category_ids": "One or more category_ids are invalid or not allowed."})

        UserNotificationPreference.objects.bulk_create(
            [UserNotificationPreference(user_id=user_id, category=cat) for cat in categories]
        )
        ids = [cat.id for cat in categories]
        cache.set(_user_cache_key(user_id), ids, CACHE_TTL_SECONDS)
        return {"mode": "custom", "category_ids": ids}


def replace_team_preferences(team_id: int, category_ids: list | None) -> dict:
    with transaction.atomic():
        TeamNotificationPreference.objects.filter(team_id=team_id).delete()

        if category_ids is None:
            cache.delete(_team_cache_key(team_id))
            return {"mode": "default", "category_ids": []}

        if len(category_ids) == 0:
            TeamNotificationPreference.objects.create(team_id=team_id, category=None)
            cache.set(_team_cache_key(team_id), [], CACHE_TTL_SECONDS)
            return {"mode": "block_all", "category_ids": []}

        categories = list(
            NotificationCategory.objects.filter(id__in=category_ids, key__in=TEAM_ALLOWED_CATEGORY_KEYS)
        )
        if len(categories) != len(set(category_ids)):
            raise ValidationError({"category_ids": "One or more category_ids are invalid or not allowed."})

        TeamNotificationPreference.objects.bulk_create(
            [TeamNotificationPreference(team_id=team_id, category=cat) for cat in categories]
        )
        ids = [cat.id for cat in categories]
        cache.set(_team_cache_key(team_id), ids, CACHE_TTL_SECONDS)
        return {"mode": "custom", "category_ids": ids}


def get_user_preference_payload(user_id: str) -> dict:
    prefs = UserNotificationPreference.objects.filter(user_id=user_id)
    if not prefs.exists():
        return {"mode": "default", "category_ids": []}
    if prefs.filter(category__isnull=True).exists():
        return {"mode": "block_all", "category_ids": []}
    return {
        "mode": "custom",
        "category_ids": list(prefs.filter(category__isnull=False).values_list("category_id", flat=True)),
    }


def get_team_preference_payload(team_id: int) -> dict:
    prefs = TeamNotificationPreference.objects.filter(team_id=team_id)
    if not prefs.exists():
        return {"mode": "default", "category_ids": []}
    if prefs.filter(category__isnull=True).exists():
        return {"mode": "block_all", "category_ids": []}
    return {
        "mode": "custom",
        "category_ids": list(prefs.filter(category__isnull=False).values_list("category_id", flat=True)),
    }


def filter_recipient_subs_by_user_preferences(recipient_subs: list[str], category_id):
    if category_id is None:
        return recipient_subs
    return [sub for sub in recipient_subs if category_id in get_eligible_categories_for_user(sub)]


def team_allows_category(team_id: int, category_id):
    key = _team_cache_key(team_id)
    cached = cache.get(key)
    if cached is None:
        prefs = TeamNotificationPreference.objects.filter(team_id=team_id)
        if not prefs.exists():
            cached = get_all_category_ids(allowed_keys=TEAM_ALLOWED_CATEGORY_KEYS)
        elif prefs.filter(category__isnull=True).exists():
            cached = []
        else:
            cached = list(prefs.filter(category__isnull=False).values_list("category_id", flat=True))
        cache.set(key, cached, CACHE_TTL_SECONDS)
    return category_id in cached
