from django.db import migrations, models
import uuid


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0002_seed_notification_categories"),
    ]

    operations = [
        migrations.CreateModel(
            name="FCMDeviceToken",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("user_sub", models.CharField(db_index=True, max_length=64)),
                ("token", models.TextField(unique=True)),
                (
                    "device_type",
                    models.CharField(
                        choices=[("web", "Web Browser"), ("android", "Android"), ("ios", "iOS")],
                        default="web",
                        max_length=20,
                    ),
                ),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="fcmdevicetoken",
            index=models.Index(fields=["user_sub", "is_active"], name="notifications_fcm_user_sub_is_active_idx"),
        ),
    ]
