from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bots", "0083_backfill_populate_bot_login_groups_from_deprecated_models"),
    ]

    operations = [
        migrations.AddField(
            model_name="botmediarequest",
            name="mute_video",
            field=models.BooleanField(db_default=False, default=False),
        ),
    ]
