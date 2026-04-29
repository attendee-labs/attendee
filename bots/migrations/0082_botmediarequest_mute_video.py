from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bots", "0081_remove_botevent_valid_event_type_event_sub_type_combinations_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="botmediarequest",
            name="mute_video",
            field=models.BooleanField(db_default=False, default=False),
        ),
    ]
