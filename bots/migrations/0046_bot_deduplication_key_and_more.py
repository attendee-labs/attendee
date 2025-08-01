# Generated by Django 5.1.2 on 2025-07-10 02:18

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0045_alter_botevent_event_sub_type_botresourcesnapshot'),
    ]

    operations = [
        migrations.AddField(
            model_name='bot',
            name='deduplication_key',
            field=models.CharField(blank=True, help_text='Optional key for deduplicating bots', max_length=1024, null=True),
        ),
        migrations.AddConstraint(
            model_name='bot',
            constraint=models.UniqueConstraint(condition=models.Q(('state__in', [7, 9, 10]), _negated=True), fields=('project', 'deduplication_key'), name='unique_bot_deduplication_key'),
        ),
    ]
