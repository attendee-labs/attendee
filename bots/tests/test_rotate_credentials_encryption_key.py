from io import StringIO
from unittest.mock import patch

from cryptography.fernet import Fernet, InvalidToken
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import models
from django.test import TestCase, override_settings
from django.utils import timezone

from bots.credentials_encryption_utils import decrypt_credentials, encrypt_credentials
from bots.management.commands.rotate_credentials_encryption_key import ENCRYPTED_COLUMNS, Command
from bots.models import Calendar, CalendarPlatform, Credentials, Organization, Project, WebhookSecret

OLD_KEY = Fernet.generate_key().decode()
NEW_KEY = Fernet.generate_key().decode()
MALFORMED_KEY = NEW_KEY[:-5]

# Binary columns that hold plain (unencrypted) data, so the rotation command skips them.
UNENCRYPTED_BINARY_COLUMNS = {
    ("bots.AudioChunk", "audio_blob"),
    ("bots.Utterance", "audio_blob"),
    ("bots.MediaBlob", "blob"),
}


class EncryptedColumnsListTest(TestCase):
    def test_every_binary_column_is_classified(self):
        """A BinaryField missing from ENCRYPTED_COLUMNS would not be re-encrypted and would become unreadable after the key swap."""
        encrypted = {(model._meta.label, field_name) for model, field_name in ENCRYPTED_COLUMNS}
        self.assertFalse(encrypted & UNENCRYPTED_BINARY_COLUMNS)

        binary_columns = {(model._meta.label, field.name) for model in apps.get_models() for field in model._meta.get_fields() if isinstance(field, models.BinaryField)}
        unclassified = binary_columns - encrypted - UNENCRYPTED_BINARY_COLUMNS
        self.assertFalse(unclassified, f"Add these BinaryFields to ENCRYPTED_COLUMNS in rotate_credentials_encryption_key.py if they are encrypted, otherwise to UNENCRYPTED_BINARY_COLUMNS in this test: {sorted(unclassified)}")

        stale = (encrypted | UNENCRYPTED_BINARY_COLUMNS) - binary_columns
        self.assertFalse(stale, f"These listed columns are no longer BinaryFields: {sorted(stale)}")


# The settings at each point of a rotation from OLD_KEY to NEW_KEY.
BEFORE_ROTATION = {"CREDENTIALS_ENCRYPTION_KEY": OLD_KEY, "CREDENTIALS_ENCRYPTION_KEY_FALLBACKS": []}
NEW_KEY_AS_FALLBACK = {"CREDENTIALS_ENCRYPTION_KEY": OLD_KEY, "CREDENTIALS_ENCRYPTION_KEY_FALLBACKS": [NEW_KEY]}
ROTATING = {"CREDENTIALS_ENCRYPTION_KEY": NEW_KEY, "CREDENTIALS_ENCRYPTION_KEY_FALLBACKS": [OLD_KEY]}
AFTER_ROTATION = {"CREDENTIALS_ENCRYPTION_KEY": NEW_KEY, "CREDENTIALS_ENCRYPTION_KEY_FALLBACKS": []}


@override_settings(**BEFORE_ROTATION)
class CredentialsEncryptionHelperTest(TestCase):
    def test_without_fallbacks_uses_current_key(self):
        token = encrypt_credentials(b"secret")
        self.assertEqual(Fernet(OLD_KEY).decrypt(token), b"secret")
        with self.assertRaises(InvalidToken):
            decrypt_credentials(Fernet(NEW_KEY).encrypt(b"new"))

    def test_fallback_equal_to_current_key_is_ignored(self):
        with override_settings(CREDENTIALS_ENCRYPTION_KEY_FALLBACKS=[OLD_KEY]):
            token = encrypt_credentials(b"secret")
        self.assertEqual(Fernet(OLD_KEY).decrypt(token), b"secret")

    def test_encrypts_with_current_key_and_decrypts_with_fallbacks(self):
        old_token = Fernet(OLD_KEY).encrypt(b"old")
        with override_settings(**ROTATING):
            new_token = encrypt_credentials(b"new")
            self.assertEqual(decrypt_credentials(old_token), b"old")
            self.assertEqual(decrypt_credentials(new_token), b"new")
        self.assertEqual(Fernet(NEW_KEY).decrypt(new_token), b"new")
        with self.assertRaises(InvalidToken):
            Fernet(OLD_KEY).decrypt(new_token)

    def test_new_key_as_fallback_reads_new_key_rows_and_keeps_writing_with_old_key(self):
        new_token = Fernet(NEW_KEY).encrypt(b"new")
        with override_settings(**NEW_KEY_AS_FALLBACK):
            self.assertEqual(decrypt_credentials(new_token), b"new")
            token = encrypt_credentials(b"secret")
        self.assertEqual(Fernet(OLD_KEY).decrypt(token), b"secret")

    def test_malformed_fallback_key_is_ignored_and_logged(self):
        old_token = Fernet(OLD_KEY).encrypt(b"old")
        with override_settings(CREDENTIALS_ENCRYPTION_KEY_FALLBACKS=[MALFORMED_KEY, NEW_KEY]), self.assertLogs("bots.credentials_encryption_utils", level="ERROR"):
            token = encrypt_credentials(b"secret")
            self.assertEqual(decrypt_credentials(old_token), b"old")
            # The valid fallback after the malformed one still works.
            self.assertEqual(decrypt_credentials(Fernet(NEW_KEY).encrypt(b"new")), b"new")
        self.assertEqual(Fernet(OLD_KEY).decrypt(token), b"secret")


@override_settings(**BEFORE_ROTATION)
class RotateCredentialsEncryptionKeyCommandTest(TestCase):
    def setUp(self):
        organization = Organization.objects.create(name="Test Organization")
        self.project = Project.objects.create(name="Test Project", organization=organization)
        self.credentials = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.DEEPGRAM)
        self.credentials.set_credentials({"api_key": "deepgram-key"})
        self.webhook_secret = WebhookSecret.objects.create(project=self.project)
        self.webhook_secret_value = self.webhook_secret.get_secret()
        self.assertIsNotNone(self.webhook_secret_value)

    def run_command(self, *args):
        out = StringIO()
        call_command("rotate_credentials_encryption_key", *args, stdout=out)
        return out.getvalue()

    def test_without_fallbacks_verifies_and_changes_nothing(self):
        original_token = bytes(Credentials.objects.get(pk=self.credentials.pk)._encrypted_data)
        self.assertIn("Rows on a fallback key: 0", self.run_command())
        self.assertEqual(bytes(Credentials.objects.get(pk=self.credentials.pk)._encrypted_data), original_token)

    @override_settings(CREDENTIALS_ENCRYPTION_KEY=NEW_KEY, CREDENTIALS_ENCRYPTION_KEY_FALLBACKS=[MALFORMED_KEY])
    def test_fails_with_malformed_fallback_key(self):
        original_token = bytes(Credentials.objects.get(pk=self.credentials.pk)._encrypted_data)
        with self.assertRaisesMessage(CommandError, "not a valid Fernet key"):
            self.run_command()
        self.assertEqual(bytes(Credentials.objects.get(pk=self.credentials.pk)._encrypted_data), original_token)

    @override_settings(**ROTATING)
    def test_check_reports_rows_on_fallback_key_without_changing_them(self):
        original_token = bytes(Credentials.objects.get(pk=self.credentials.pk)._encrypted_data)
        with self.assertRaises(CommandError):
            self.run_command("--check")
        self.assertEqual(bytes(Credentials.objects.get(pk=self.credentials.pk)._encrypted_data), original_token)

    def test_full_rotation(self):
        updated_at_before = Credentials.objects.get(pk=self.credentials.pk).updated_at

        with override_settings(**ROTATING):
            # Rows on the previous key stay readable before the command has run.
            self.assertEqual(Credentials.objects.get(pk=self.credentials.pk).get_credentials(), {"api_key": "deepgram-key"})

            # A row written after the key change.
            openai = Credentials.objects.create(project=self.project, credential_type=Credentials.CredentialTypes.OPENAI)
            openai.set_credentials({"api_key": "openai-key"})
            openai_token = bytes(Credentials.objects.get(pk=openai.pk)._encrypted_data)

            output = self.run_command()
            self.assertIn("Rows on a fallback key: 0", output)
            self.run_command("--check")

        credentials = Credentials.objects.get(pk=self.credentials.pk)
        self.assertEqual(credentials.updated_at, updated_at_before)
        self.assertEqual(Fernet(NEW_KEY).decrypt(bytes(credentials._encrypted_data)), b'{"api_key": "deepgram-key"}')
        self.assertEqual(Fernet(NEW_KEY).decrypt(bytes(WebhookSecret.objects.get(pk=self.webhook_secret.pk)._secret)), self.webhook_secret_value)
        # Rows already on the new key are left as they are.
        self.assertEqual(bytes(Credentials.objects.get(pk=openai.pk)._encrypted_data), openai_token)

        # Final step: the fallback is removed.
        with override_settings(**AFTER_ROTATION):
            self.assertEqual(Credentials.objects.get(pk=self.credentials.pk).get_credentials(), {"api_key": "deepgram-key"})
            self.assertEqual(Credentials.objects.get(pk=openai.pk).get_credentials(), {"api_key": "openai-key"})
            self.assertEqual(WebhookSecret.objects.get(pk=self.webhook_secret.pk).get_secret(), self.webhook_secret_value)
            self.run_command("--check")

    def test_stale_save_after_rotation_stays_readable_and_is_reported(self):
        calendar = Calendar.objects.create(project=self.project, platform=CalendarPlatform.GOOGLE, client_id="client-id")
        calendar.set_credentials({"refresh_token": "refresh-token"})

        with override_settings(**ROTATING):
            # A task loads the row, the command re-encrypts it, then the task saves the whole row.
            in_flight_calendar = Calendar.objects.get(pk=calendar.pk)
            self.run_command()
            in_flight_calendar.last_attempted_sync_at = timezone.now()
            in_flight_calendar.save()

            self.assertEqual(Calendar.objects.get(pk=calendar.pk).get_credentials(), {"refresh_token": "refresh-token"})
            with self.assertRaises(CommandError):
                self.run_command("--check")
            self.run_command()
            self.run_command("--check")

    @override_settings(**ROTATING)
    def test_row_becoming_undecryptable_after_rotation_fails_verification(self):
        command = Command()
        process_column = command._process_column
        unknown_key_token = Fernet(Fernet.generate_key()).encrypt(b"{}")

        def process_with_concurrent_write(model, field_name, new_fernet, rotation_fernet, check_only):
            counts = process_column(model, field_name, new_fernet, rotation_fernet, check_only)
            if model is Credentials and not check_only:
                Credentials.objects.filter(pk=self.credentials.pk).update(_encrypted_data=unknown_key_token)
            return counts

        out = StringIO()
        with patch.object(command, "_process_column", side_effect=process_with_concurrent_write), self.assertRaises(CommandError):
            call_command(command, stdout=out)
        self.assertIn("Rows on a fallback key: 0. Undecryptable rows: 1.", out.getvalue())

    @override_settings(**ROTATING)
    def test_undecryptable_row_fails_the_command(self):
        unknown_key_token = Fernet(Fernet.generate_key()).encrypt(b"{}")
        Credentials.objects.filter(pk=self.credentials.pk).update(_encrypted_data=unknown_key_token)
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual(bytes(Credentials.objects.get(pk=self.credentials.pk)._encrypted_data), unknown_key_token)
        # The other rows were still rotated.
        self.assertEqual(Fernet(NEW_KEY).decrypt(bytes(WebhookSecret.objects.get(pk=self.webhook_secret.pk)._secret)), self.webhook_secret_value)
