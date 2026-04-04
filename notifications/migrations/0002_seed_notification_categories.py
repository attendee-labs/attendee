from django.db import migrations


CATEGORIES = [
    ("event_reminder", "Event Reminder", "Reminders for upcoming events."),
    ("teams", "Teams", "Team-related updates and mentions."),
    ("meeting_summary", "Meeting Summary", "Summary notifications after meetings."),
    ("comments", "Comments", "New comments and replies."),
    ("clips", "Clips", "Clip creation and sharing updates."),
    ("product_updates", "Product Updates", "Announcements about product improvements."),
    ("workspace_members", "Workspace Members", "Workspace member and access updates."),
    ("meetings", "Meetings", "Meeting-related notifications."),
    ("action_items", "Action Items", "Action item assignments and updates."),
    ("members", "Members", "Team member lifecycle updates."),
]


def seed_categories(apps, schema_editor):
    NotificationCategory = apps.get_model("notifications", "NotificationCategory")
    for key, label, description in CATEGORIES:
        NotificationCategory.objects.update_or_create(
            key=key,
            defaults={"label": label, "description": description},
        )


def unseed_categories(apps, schema_editor):
    NotificationCategory = apps.get_model("notifications", "NotificationCategory")
    NotificationCategory.objects.filter(key__in=[item[0] for item in CATEGORIES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_categories, unseed_categories),
    ]
