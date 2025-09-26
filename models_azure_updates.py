"""
Azure Storage support - Database model considerations

This file outlines potential database model updates to fully support
Azure Storage configurations alongside AWS S3.

Note: These changes are optional and depend on your specific requirements.
The current implementation works with environment variables only.
"""

# Potential model field additions for per-project or per-bot Azure configuration:

"""
# In bots/models.py - Bot model additions
class Bot(models.Model):
    # ... existing fields ...
    
    # Azure Storage Configuration (optional)
    azure_storage_account_name = models.CharField(max_length=200, blank=True, null=True)
    azure_storage_container_name = models.CharField(max_length=200, blank=True, null=True)
    azure_storage_connection_string = models.TextField(blank=True, null=True)  # Encrypted
    
    # Storage behavior control
    STORAGE_MODES = [
        ('s3', 'AWS S3 Only'),
        ('azure', 'Azure Storage Only'),
        ('both', 'Both AWS S3 and Azure Storage'),
    ]
    storage_upload_mode = models.CharField(max_length=10, choices=STORAGE_MODES, default='s3')
    
    def azure_storage_enabled(self):
        return self.storage_upload_mode in ['azure', 'both']
    
    def aws_s3_enabled(self):
        return self.storage_upload_mode in ['s3', 'both']
"""

"""
# In accounts/models.py - Project model additions  
class Project(models.Model):
    # ... existing fields ...
    
    # Azure Storage defaults for bots in this project
    default_azure_storage_account_name = models.CharField(max_length=200, blank=True, null=True)
    default_azure_storage_container_name = models.CharField(max_length=200, blank=True, null=True)
    default_storage_upload_mode = models.CharField(max_length=10, default='s3')
"""

"""
# Credentials model extensions
class Credentials(models.Model):
    # ... existing fields ...
    
    class CredentialTypes(models.TextChoices):
        # ... existing types ...
        AZURE_STORAGE = "azure_storage", "Azure Storage"
    
    # The credential_data field would store:
    # {
    #     "storage_account_name": "...",
    #     "connection_string": "...",  # encrypted
    #     "account_key": "...",        # encrypted
    #     "container_name": "..."
    # }
"""

# Migration considerations:
"""
# migrations/XXXX_add_azure_storage_support.py
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [
        ('bots', 'previous_migration'),
    ]
    
    operations = [
        migrations.AddField(
            model_name='bot',
            name='azure_storage_account_name',
            field=models.CharField(blank=True, max_length=200, null=True),
        ),
        migrations.AddField(
            model_name='bot',
            name='azure_storage_container_name', 
            field=models.CharField(blank=True, max_length=200, null=True),
        ),
        migrations.AddField(
            model_name='bot',
            name='storage_upload_mode',
            field=models.CharField(
                choices=[('s3', 'AWS S3 Only'), ('azure', 'Azure Storage Only'), ('both', 'Both AWS S3 and Azure Storage')],
                default='s3',
                max_length=10
            ),
        ),
    ]
"""

# Admin interface updates:
"""
# In bots/admin.py
from django.contrib import admin
from .models import Bot

class BotAdmin(admin.ModelAdmin):
    fieldsets = [
        # ... existing fieldsets ...
        ('Storage Configuration', {
            'fields': ['storage_upload_mode', 'azure_storage_account_name', 'azure_storage_container_name'],
            'classes': ['collapse'],
        }),
    ]
    
    list_display = [..., 'storage_upload_mode']
    list_filter = [..., 'storage_upload_mode']
"""

# Template updates for UI:
"""
<!-- In bot configuration templates -->
<div class="form-group">
    <label for="storage_upload_mode">Storage Upload Mode</label>
    <select id="storage_upload_mode" name="storage_upload_mode" class="form-control">
        <option value="s3">AWS S3 Only</option>
        <option value="azure">Azure Storage Only</option> 
        <option value="both">Both AWS S3 and Azure Storage</option>
    </select>
</div>

<div id="azure-storage-config" class="azure-storage-fields">
    <div class="form-group">
        <label for="azure_storage_account_name">Azure Storage Account Name</label>
        <input type="text" id="azure_storage_account_name" name="azure_storage_account_name" class="form-control">
    </div>
    <div class="form-group">
        <label for="azure_storage_container_name">Azure Storage Container Name</label>
        <input type="text" id="azure_storage_container_name" name="azure_storage_container_name" class="form-control">
    </div>
</div>
"""
