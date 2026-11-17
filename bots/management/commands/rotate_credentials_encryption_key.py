import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from bots.credentials_encryption_utils import configured_fallback_credentials_encryption_keys, get_credentials_fernet, is_valid_fernet_key
from bots.models import BotLogin, Calendar, Credentials, GoogleMeetBotLogin, WebexServiceApp, WebhookSecret, ZoomOAuthApp, ZoomOAuthConnection

logger = logging.getLogger(__name__)

# Every column encrypted with CREDENTIALS_ENCRYPTION_KEY.
ENCRYPTED_COLUMNS = [
    (GoogleMeetBotLogin, "_encrypted_data"),
    (BotLogin, "_encrypted_data"),
    (ZoomOAuthApp, "_encrypted_data"),
    (WebexServiceApp, "_encrypted_data"),
    (ZoomOAuthConnection, "_encrypted_data"),
    (Calendar, "_encrypted_data"),
    (Credentials, "_encrypted_data"),
    (WebhookSecret, "_secret"),
]


class Command(BaseCommand):
    help = "Re-encrypts every encrypted column that is still on a key from CREDENTIALS_ENCRYPTION_KEY_FALLBACKS with CREDENTIALS_ENCRYPTION_KEY. See bots/credentials_encryption_utils.py for the full rotation procedure."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Change nothing; report how many rows are not encrypted with CREDENTIALS_ENCRYPTION_KEY.",
        )

    def handle(self, *args, **options):
        if not settings.CREDENTIALS_ENCRYPTION_KEY:
            raise CommandError("CREDENTIALS_ENCRYPTION_KEY must be set.")
        if not all(is_valid_fernet_key(key) for key in configured_fallback_credentials_encryption_keys()):
            raise CommandError("CREDENTIALS_ENCRYPTION_KEY_FALLBACKS contains a key that is not a valid Fernet key. Pods are ignoring it, so rows encrypted with it cannot be read. Fix the value and deploy it, then re-run this command.")

        # With no fallback keys there is nothing to re-encrypt, but the command still verifies that
        # every row can be decrypted with CREDENTIALS_ENCRYPTION_KEY.
        new_fernet = Fernet(settings.CREDENTIALS_ENCRYPTION_KEY)
        rotation_fernet = get_credentials_fernet()
        check_only = options["check"]

        if not check_only:
            for model, field_name in ENCRYPTED_COLUMNS:
                rotated, _, undecryptable = self._process_column(model, field_name, new_fernet, rotation_fernet, check_only=False)
                self.stdout.write(f"{model.__name__}.{field_name}: re-encrypted {rotated} rows, {undecryptable} rows undecryptable")

        # A concurrent save can change a row after rotation. Use the verification pass
        # for both counts so the summary and exit status reflect what it found.
        total_remaining = 0
        total_undecryptable = 0
        for model, field_name in ENCRYPTED_COLUMNS:
            _, remaining, undecryptable = self._process_column(model, field_name, new_fernet, rotation_fernet, check_only=True)
            total_remaining += remaining
            total_undecryptable += undecryptable
            if check_only:
                self.stdout.write(f"{model.__name__}.{field_name}: {remaining} rows on a fallback key, {undecryptable} rows undecryptable")

        self.stdout.write(f"Rows on a fallback key: {total_remaining}. Undecryptable rows: {total_undecryptable}.")
        if total_remaining or total_undecryptable:
            raise CommandError("Not every row is encrypted with CREDENTIALS_ENCRYPTION_KEY. Do not remove CREDENTIALS_ENCRYPTION_KEY_FALLBACKS yet; re-run this command and investigate any undecryptable rows.")
        self.stdout.write(self.style.SUCCESS("Every row is encrypted with CREDENTIALS_ENCRYPTION_KEY. Before removing CREDENTIALS_ENCRYPTION_KEY_FALLBACKS, wait until no pod from before the key change is running and confirm this again with --check."))

    def _process_column(self, model, field_name, new_fernet, rotation_fernet, check_only):
        rotated = 0
        remaining = 0
        undecryptable = 0
        pks = model.objects.exclude(**{f"{field_name}__isnull": True}).values_list("pk", flat=True).order_by("pk")
        for pk in pks.iterator():
            if check_only:
                token = model.objects.filter(pk=pk).values_list(field_name, flat=True).first()
                status = self._key_status(token, new_fernet, rotation_fernet)
            else:
                # Lock the row so a concurrent write between our read and our update is not lost.
                with transaction.atomic():
                    token = model.objects.select_for_update().filter(pk=pk).values_list(field_name, flat=True).first()
                    status = self._key_status(token, new_fernet, rotation_fernet)
                    if status == "old":
                        new_token = new_fernet.encrypt(rotation_fernet.decrypt(bytes(token)))
                        # update() skips save(), so updated_at and version fields are left alone.
                        model.objects.filter(pk=pk).update(**{field_name: new_token})
                        rotated += 1
                        status = "new"

            if status == "old":
                remaining += 1
            elif status == "undecryptable":
                undecryptable += 1
                logger.error(f"rotate_credentials_encryption_key: {model.__name__} pk={pk} {field_name} cannot be decrypted with any configured key")
        return rotated, remaining, undecryptable

    def _key_status(self, token, new_fernet, rotation_fernet):
        if not token:
            return "empty"
        token = bytes(token)
        try:
            new_fernet.decrypt(token)
            return "new"
        except InvalidToken:
            pass
        try:
            rotation_fernet.decrypt(token)
            return "old"
        except InvalidToken:
            return "undecryptable"
