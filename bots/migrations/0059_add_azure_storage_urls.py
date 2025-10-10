# Generated manually for Azure Storage URL support

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bots', '0058_alter_webhookdeliveryattempt_webhook_trigger_type_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='recording',
            name='azure_blob_url',
            field=models.URLField(max_length=2048, null=True, blank=True, help_text='Azure Blob Storage URL for this recording'),
        ),
        migrations.AddField(
            model_name='botdebugscreenshot',
            name='azure_blob_url',
            field=models.URLField(max_length=2048, null=True, blank=True, help_text='Azure Blob Storage URL for this debug screenshot'),
        ),
    ]
