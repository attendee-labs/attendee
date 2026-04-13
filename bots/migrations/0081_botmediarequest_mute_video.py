from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bots", "0080_alter_mediablob_content_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="botmediarequest",
            name="mute_video",
            field=models.BooleanField(db_default=False, default=False),
        ),
    ]
