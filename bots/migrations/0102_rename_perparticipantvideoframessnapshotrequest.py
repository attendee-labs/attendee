import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0101_perparticipantvideoframessnapshotrequest'),
    ]

    operations = [
        migrations.AlterModelTable(
            name='perparticipantvideoframessnapshotrequest',
            table='bots_perparticipantvideoframessnapshotrequest',
        ),
        migrations.RenameModel(
            old_name='PerParticipantVideoFramesSnapshotRequest',
            new_name='PerParticipantVideoSnapshotRequest',
        ),
        migrations.AlterField(
            model_name='perparticipantvideosnapshotrequest',
            name='bot',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='per_participant_video_snapshot_requests', to='bots.bot'),
        ),
    ]
