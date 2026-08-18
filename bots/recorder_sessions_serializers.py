from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import (
    RecorderUploadStates,
    Recording,
    RecordingStates,
)


class CreateRecorderSessionSerializer(serializers.Serializer):
    content_type = serializers.CharField(required=False, default="video/mp4", help_text="MIME type of the media being uploaded, e.g. video/mp4, video/webm, audio/mpeg.")
    metadata = serializers.JSONField(required=False, help_text="Arbitrary JSON metadata to attach to the session.")
    deduplication_key = serializers.CharField(required=False, allow_null=True, allow_blank=False, max_length=1024, help_text="Reusing this key returns the existing active session instead of creating a duplicate.")
    bytes_expected = serializers.IntegerField(required=False, allow_null=True, min_value=0, help_text="Total expected upload size in bytes, if known.")
    num_parts = serializers.IntegerField(required=False, min_value=1, max_value=10000, default=1, help_text="How many presigned multipart part-upload URLs to return.")


class PartUploadUrlsSerializer(serializers.Serializer):
    part_numbers = serializers.ListField(child=serializers.IntegerField(min_value=1, max_value=10000), allow_empty=False, help_text="Part numbers to issue presigned upload URLs for.")


class CompletePartSerializer(serializers.Serializer):
    part_number = serializers.IntegerField(min_value=1, max_value=10000)
    etag = serializers.CharField(max_length=255)


class CompleteRecorderSessionSerializer(serializers.Serializer):
    parts = CompletePartSerializer(many=True, required=False, help_text="Uploaded parts. If omitted, the server resumes from the parts S3 has received.")


class PartUploadUrlSerializer(serializers.Serializer):
    part_number = serializers.IntegerField()
    url = serializers.CharField()


class RecorderSessionSerializer(serializers.Serializer):
    """Stable representation of a recorder session (the RecorderUpload instance).

    Dynamic upload details (presigned part URLs, part size) are injected via context under
    the `upload` key on create / parts responses.
    """

    id = serializers.CharField(source="bot.object_id")
    object_id = serializers.CharField()
    state = serializers.SerializerMethodField()
    content_type = serializers.CharField()
    bytes_expected = serializers.IntegerField(allow_null=True)
    bytes_received = serializers.IntegerField()
    deduplication_key = serializers.CharField(allow_null=True)
    recording_state = serializers.SerializerMethodField()
    received_parts = serializers.SerializerMethodField()
    upload = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()

    def get_state(self, obj):
        return RecorderUploadStates.state_to_api_code(obj.state)

    def get_recording_state(self, obj):
        recording = Recording.objects.filter(bot_id=obj.bot_id, is_default_recording=True).first()
        if not recording:
            return None
        return RecordingStates.state_to_api_code(recording.state)

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_received_parts(self, obj):
        # Only surfaced when the caller asks for resume info (status endpoint).
        return self.context.get("received_parts")

    @extend_schema_field(OpenApiTypes.OBJECT)
    def get_upload(self, obj):
        upload = self.context.get("upload")
        if upload is None:
            return None
        return {
            "upload_id": obj.upload_id,
            "part_size_bytes": upload.get("part_size_bytes"),
            "url_expiry_seconds": upload.get("url_expiry_seconds"),
            "part_urls": PartUploadUrlSerializer(upload.get("part_urls", []), many=True).data,
        }


class AccountSerializer(serializers.Serializer):
    """Response for GET /api/v1/account: identifies the API key's project + organization."""

    project = serializers.SerializerMethodField()
    organization = serializers.SerializerMethodField()
    api_key = serializers.SerializerMethodField()

    def get_project(self, obj):
        project = obj["project"]
        return {"object_id": project.object_id, "name": project.name}

    def get_organization(self, obj):
        organization = obj["project"].organization
        return {
            "name": organization.name,
            "credits": organization.credits(),
            "is_app_sessions_enabled": organization.is_app_sessions_enabled,
        }

    def get_api_key(self, obj):
        api_key = obj["api_key"]
        return {"name": api_key.name, "object_id": api_key.object_id}
